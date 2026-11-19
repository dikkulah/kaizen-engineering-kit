use crate::git_forensics::normalize_rel_path;
use crate::schema::CodeNode;
use anyhow::Result;
use rayon::prelude::*;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use tree_sitter::{Node, Parser};
use walkdir::WalkDir;

fn should_skip_path(norm_path: &str) -> bool {
    let wrapped = format!("/{norm_path}");
    wrapped.contains("/target/")
        || wrapped.contains("/build/")
        || wrapped.contains("/vendor/")
        || wrapped.contains("/node_modules/")
        || wrapped.contains("/.git/")
        || wrapped.contains("/.venv")
}

pub fn extract_ast_nodes(
    repo_root: &Path,
    service_name: &str,
    file_churn: &HashMap<String, u32>,
) -> Result<Vec<CodeNode>> {
    let files: Vec<_> = WalkDir::new(repo_root)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| {
            let rel = e
                .path()
                .strip_prefix(repo_root)
                .unwrap_or(e.path())
                .to_string_lossy();
            let norm = normalize_rel_path(&rel);
            !should_skip_path(&norm)
        })
        .collect();

    let mut nodes: Vec<CodeNode> = files
        .par_iter()
        .flat_map(|entry| {
            let path = entry.path();
            let ext = path.extension().and_then(|s| s.to_str()).unwrap_or("");
            let lang_name = match ext {
                "rs" => "rust",
                "java" => "java",
                "go" => "go",
                "ts" | "tsx" | "js" | "jsx" | "vue" => "typescript",
                _ => return Vec::new(),
            };
            let Ok(source) = fs::read_to_string(path) else {
                return Vec::new();
            };
            let effective_source = if ext == "vue" {
                extract_vue_script_preserving_lines(&source)
            } else {
                source
            };
            let rel_raw = path
                .strip_prefix(repo_root)
                .unwrap_or(path)
                .to_string_lossy();
            let rel_path = normalize_rel_path(&rel_raw);
            let churn = *file_churn.get(&rel_path).unwrap_or(&0);
            parse_file_functions(&effective_source, &rel_path, lang_name, service_name, churn)
                .unwrap_or_default()
        })
        .collect();

    nodes.sort_by(|a, b| {
        a.file_path
            .cmp(&b.file_path)
            .then_with(|| a.start_line.cmp(&b.start_line))
    });
    Ok(nodes)
}

pub fn parse_file_functions(
    source: &str,
    rel_path: &str,
    lang: &str,
    service_name: &str,
    churn: u32,
) -> Result<Vec<CodeNode>> {
    let mut parser = Parser::new();
    match lang {
        "rust" => parser.set_language(&tree_sitter_rust::LANGUAGE.into())?,
        "java" => parser.set_language(&tree_sitter_java::LANGUAGE.into())?,
        "go" => parser.set_language(&tree_sitter_go::LANGUAGE.into())?,
        "typescript" => parser.set_language(&tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into())?,
        _ => return Ok(vec![]),
    }
    let Some(tree) = parser.parse(source, None) else {
        return Ok(vec![]);
    };
    let mut out = Vec::new();
    collect_callables(
        tree.root_node(),
        source.as_bytes(),
        rel_path,
        lang,
        service_name,
        churn,
        None,
        &mut out,
    );
    Ok(out)
}

fn find_enclosing_name(node: Node, src: &[u8], lang: &str, current_scope: Option<&str>) -> Option<String> {
    let kind = node.kind();
    match (lang, kind) {
        ("rust", "impl_item") => node
            .child_by_field_name("type")
            .and_then(|n| n.utf8_text(src).ok())
            .map(|s| s.split('<').next().unwrap_or(s).trim().to_string()),
        ("java", "class_declaration" | "record_declaration" | "enum_declaration" | "interface_declaration") => {
            node.child_by_field_name("name")
                .and_then(|n| n.utf8_text(src).ok())
                .map(|s| s.trim().to_string())
        }
        ("typescript", "class_declaration" | "interface_declaration" | "class") => {
            node.child_by_field_name("name")
                .and_then(|n| n.utf8_text(src).ok())
                .map(|s| s.trim().to_string())
        }
        _ => current_scope.map(|s| s.to_string()),
    }
}

fn go_receiver_type(node: Node, src: &[u8]) -> Option<String> {
    let recv = node.child_by_field_name("receiver")?;
    let text = recv.utf8_text(src).ok()?;
    let stripped = text.trim_matches(|c| c == '(' || c == ')').trim();
    let last_tok = stripped.split_whitespace().last()?;
    Some(last_tok.trim_start_matches('*').to_string())
}

fn strip_comments_preserving_logic(body: &str) -> String {
    let mut lines = Vec::new();
    let mut in_block_comment = false;
    for raw_line in body.lines() {
        let trimmed = raw_line.trim();
        if in_block_comment {
            if let Some((_, rest)) = trimmed.split_once("*/") {
                in_block_comment = false;
                let rem = rest.trim();
                if !rem.is_empty() {
                    lines.push(raw_line);
                }
            }
            continue;
        }
        if trimmed.starts_with("//") {
            continue;
        }
        if trimmed.starts_with("/*") {
            if !trimmed.contains("*/") {
                in_block_comment = true;
            }
            continue;
        }
        if !trimmed.is_empty() {
            lines.push(raw_line);
        }
    }
    lines.join("\n")
}

#[allow(clippy::too_many_arguments)]
fn collect_callables(
    node: Node,
    src: &[u8],
    rel_path: &str,
    lang: &str,
    service_name: &str,
    churn: u32,
    scope: Option<&str>,
    out: &mut Vec<CodeNode>,
) {
    let kind = node.kind();
    let is_callable = match lang {
        "rust" => kind == "function_item",
        "java" => matches!(kind, "method_declaration" | "constructor_declaration"),
        "go" => matches!(kind, "function_declaration" | "method_declaration"),
        "typescript" => matches!(
            kind,
            "function_declaration"
                | "method_definition"
                | "arrow_function"
                | "function_expression"
                | "generator_function_declaration"
        ),
        _ => false,
    };

    if is_callable {
        let start_line = node.start_position().row as u32 + 1;
        let end_line = node.end_position().row as u32 + 1;
        if end_line.saturating_sub(start_line) >= 3 {
            let raw_name = node
                .child_by_field_name("name")
                .and_then(|n| n.utf8_text(src).ok())
                .unwrap_or("anonymous")
                .trim()
                .to_string();

            let qualified_name = match lang {
                "rust" => match scope {
                    Some(s) if !s.is_empty() => format!("{s}::{raw_name}"),
                    _ => raw_name,
                },
                "java" | "typescript" => match scope {
                    Some(s) if !s.is_empty() => format!("{s}.{raw_name}"),
                    _ => raw_name,
                },
                "go" if kind == "method_declaration" => match go_receiver_type(node, src) {
                    Some(r) if !r.is_empty() => format!("{r}.{raw_name}"),
                    _ => raw_name,
                },
                _ => raw_name,
            };

            let raw_body = node.utf8_text(src).unwrap_or("");
            let clean_body = strip_comments_preserving_logic(raw_body);
            if clean_body.lines().count() >= 4 {
                let complexity = estimate_cognitive_complexity(node);
                let mut hasher = Sha256::new();
                hasher.update(format!("{service_name}:{rel_path}:{qualified_name}:{start_line}"));
                let symbol_id = hex::encode(&hasher.finalize()[..12]);
                let hotspot_score = ((complexity as f64).ln_1p()) * ((churn as f64 + 1.0).ln());
                let hotspot_rounded = (hotspot_score * 1000.0).round() / 1000.0;

                out.push(CodeNode {
                    symbol_id,
                    service_name: service_name.to_string(),
                    language: lang.to_string(),
                    kind: kind.to_string(),
                    name: qualified_name,
                    file_path: rel_path.to_string(),
                    start_line,
                    end_line,
                    complexity,
                    fan_in: 0,
                    fan_out: 0,
                    churn_90d: churn,
                    hotspot_score: hotspot_rounded,
                    clean_body,
                });
            }
        }
    }

    let next_scope = find_enclosing_name(node, src, lang, scope);
    let scope_ref = next_scope.as_deref();
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_callables(child, src, rel_path, lang, service_name, churn, scope_ref, out);
    }
}

pub fn estimate_cognitive_complexity(node: Node) -> u32 {
    let mut score = 1u32;
    fn walk(n: Node, depth: u32, acc: &mut u32) {
        let branching = matches!(
            n.kind(),
            "if_expression"
                | "if_statement"
                | "match_expression"
                | "switch_expression"
                | "switch_statement"
                | "expression_switch_statement"
                | "type_switch_statement"
                | "select_statement"
                | "for_expression"
                | "for_statement"
                | "enhanced_for_statement"
                | "while_expression"
                | "while_statement"
                | "do_statement"
                | "loop_expression"
                | "catch_clause"
                | "ternary_expression"
        );
        let next_depth = if branching {
            *acc += 1 + depth;
            depth + 1
        } else {
            depth
        };
        let mut c = n.walk();
        for child in n.children(&mut c) {
            walk(child, next_depth, acc);
        }
    }
    walk(node, 0, &mut score);
    score
}

/// Extracts `<script...>` block from a Vue SFC, filling other lines with empty newlines
/// so line numbers match the original .vue file perfectly.
fn extract_vue_script_preserving_lines(source: &str) -> String {
    let mut script_lines = Vec::new();
    let mut in_script = false;

    for line in source.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("<script") {
            in_script = true;
            script_lines.push("");
            continue;
        }
        if trimmed.starts_with("</script>") {
            in_script = false;
            script_lines.push("");
            continue;
        }
        if in_script {
            script_lines.push(line);
        } else {
            script_lines.push("");
        }
    }
    script_lines.join("\n")
}

