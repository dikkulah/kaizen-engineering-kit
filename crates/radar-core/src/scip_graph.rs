use crate::git_forensics::normalize_rel_path;
use crate::schema::{CodeEdge, CodeNode};
use anyhow::Result;
use protobuf::Message;
use scip::types::{Index, SymbolRole};
use std::collections::HashMap;
use std::fs::File;
use std::path::Path;

pub fn enrich_with_scip(scip_file: &Path, nodes: &mut [CodeNode]) -> Result<Vec<CodeEdge>> {
    if !scip_file.exists() {
        return Ok(vec![]);
    }
    let mut file = File::open(scip_file)?;
    let index = Index::parse_from_reader(&mut file)?;

    let mut file_map: HashMap<String, Vec<usize>> = HashMap::new();
    for (idx, n) in nodes.iter().enumerate() {
        file_map.entry(n.file_path.clone()).or_default().push(idx);
    }

    let mut scip_def_to_node: HashMap<String, String> = HashMap::new();
    let mut raw_refs: Vec<(String, String)> = Vec::new();
    let mut impl_edges: Vec<(String, String)> = Vec::new();

    for doc in &index.documents {
        let rel = normalize_rel_path(&doc.relative_path);
        let Some(node_indices) = file_map.get(&rel) else {
            continue;
        };

        for sym_info in &doc.symbols {
            if let Some(def_node_id) = scip_def_to_node.get(&sym_info.symbol) {
                for rel_item in &sym_info.relationships {
                    if rel_item.is_implementation {
                        impl_edges.push((def_node_id.clone(), rel_item.symbol.clone()));
                    }
                }
            }
        }

        for occ in &doc.occurrences {
            if occ.range.is_empty() || occ.symbol.starts_with("local ") {
                continue;
            }
            let line = occ.range[0] as u32 + 1;
            let enclosing = node_indices
                .iter()
                .copied()
                .filter(|&i| nodes[i].start_line <= line && line <= nodes[i].end_line)
                .min_by_key(|&i| nodes[i].end_line.saturating_sub(nodes[i].start_line));
            let Some(node_idx) = enclosing else {
                continue;
            };
            let node_id = nodes[node_idx].symbol_id.clone();
            let is_def = (occ.symbol_roles & (SymbolRole::Definition as i32)) != 0;
            if is_def {
                scip_def_to_node.insert(occ.symbol.clone(), node_id);
            } else {
                raw_refs.push((node_id, occ.symbol.clone()));
            }
        }

        for sym_info in &doc.symbols {
            if let Some(def_node_id) = scip_def_to_node.get(&sym_info.symbol) {
                for rel_item in &sym_info.relationships {
                    if rel_item.is_implementation {
                        impl_edges.push((def_node_id.clone(), rel_item.symbol.clone()));
                    }
                }
            }
        }
    }

    let mut edge_weights: HashMap<(String, String, String), f64> = HashMap::new();
    let mut fan_in_map: HashMap<String, u32> = HashMap::new();
    let mut fan_out_map: HashMap<String, u32> = HashMap::new();

    for (caller_id, target_scip_sym) in raw_refs {
        if let Some(callee_id) = scip_def_to_node.get(&target_scip_sym) {
            if &caller_id != callee_id {
                let key = (caller_id.clone(), callee_id.clone(), "CALLS".to_string());
                let entry = edge_weights.entry(key).or_insert(0.0);
                if *entry == 0.0 {
                    *fan_out_map.entry(caller_id).or_insert(0) += 1;
                    *fan_in_map.entry(callee_id.clone()).or_insert(0) += 1;
                }
                *entry += 1.0;
            }
        }
    }

    for (impl_node_id, iface_scip_sym) in impl_edges {
        if let Some(iface_node_id) = scip_def_to_node.get(&iface_scip_sym) {
            if &impl_node_id != iface_node_id {
                edge_weights
                    .entry((impl_node_id, iface_node_id.clone(), "IMPLEMENTS".to_string()))
                    .or_insert(1.0);
            }
        }
    }

    for n in nodes.iter_mut() {
        n.fan_in = *fan_in_map.get(&n.symbol_id).unwrap_or(&0);
        n.fan_out = *fan_out_map.get(&n.symbol_id).unwrap_or(&0);
    }

    let mut edges: Vec<CodeEdge> = edge_weights
        .into_iter()
        .map(|((source_id, target_id, edge_type), weight)| CodeEdge {
            source_id,
            target_id,
            edge_type,
            weight,
        })
        .collect();
    edges.sort_by(|a, b| {
        a.source_id
            .cmp(&b.source_id)
            .then_with(|| a.target_id.cmp(&b.target_id))
    });
    Ok(edges)
}

pub fn attach_co_change_edges(
    co_changes: &[(String, String, f64)],
    nodes: &[CodeNode],
    edges: &mut Vec<CodeEdge>,
) {
    let mut top_symbol_by_file: HashMap<&str, &CodeNode> = HashMap::new();
    for n in nodes {
        top_symbol_by_file
            .entry(n.file_path.as_str())
            .and_modify(|cur| {
                if n.hotspot_score > cur.hotspot_score || (n.hotspot_score == cur.hotspot_score && n.complexity > cur.complexity) {
                    *cur = n;
                }
            })
            .or_insert(n);
    }

    for (file_a, file_b, weight) in co_changes {
        if let (Some(node_a), Some(node_b)) = (
            top_symbol_by_file.get(file_a.as_str()),
            top_symbol_by_file.get(file_b.as_str()),
        ) {
            if node_a.symbol_id != node_b.symbol_id {
                edges.push(CodeEdge {
                    source_id: node_a.symbol_id.clone(),
                    target_id: node_b.symbol_id.clone(),
                    edge_type: "CO_CHANGES_WITH".to_string(),
                    weight: *weight,
                });
            }
        }
    }
}

