use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::Path;
use std::process::Command;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GitForensics {
    pub file_churn: HashMap<String, u32>,
    pub co_changes: Vec<(String, String, f64)>,
    pub recent_tickets: HashMap<String, Vec<String>>,
}

pub fn normalize_rel_path(raw: &str) -> String {
    raw.trim().replace('\\', "/").trim_start_matches("./").to_string()
}

fn is_code_file(path: &str) -> bool {
    path.ends_with(".rs") || path.ends_with(".java") || path.ends_with(".go")
}

fn extract_ticket_keys(subject: &str) -> Vec<String> {
    let mut out = BTreeSet::new();
    let bytes = subject.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i].is_ascii_uppercase() {
            let start = i;
            while i < bytes.len() && bytes[i].is_ascii_uppercase() {
                i += 1;
            }
            let prefix_len = i - start;
            if (2..=8).contains(&prefix_len) && i < bytes.len() && bytes[i] == b'-' {
                let dash = i;
                i += 1;
                let num_start = i;
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                }
                if i > num_start {
                    let prev_ok = start == 0 || !bytes[start - 1].is_ascii_alphanumeric();
                    let next_ok = i == bytes.len() || !bytes[i].is_ascii_alphanumeric();
                    if prev_ok && next_ok {
                        out.insert(subject[start..i].to_string());
                        continue;
                    }
                }
                i = dash + 1;
                continue;
            }
        }
        i += 1;
    }
    out.into_iter().collect()
}

pub fn parse_git_log_output(raw: &str) -> GitForensics {
    let mut file_churn: HashMap<String, u32> = HashMap::new();
    let mut pair_counts: HashMap<(String, String), u32> = HashMap::new();
    let mut ticket_files: HashMap<String, BTreeSet<String>> = HashMap::new();

    let mut current_tickets: Vec<String> = Vec::new();
    let mut current_files: BTreeSet<String> = BTreeSet::new();

    let flush_commit = |tickets: &[String],
                        files: &BTreeSet<String>,
                        churn: &mut HashMap<String, u32>,
                        pairs: &mut HashMap<(String, String), u32>,
                        tmap: &mut HashMap<String, BTreeSet<String>>| {
        if files.is_empty() {
            return;
        }
        for f in files {
            *churn.entry(f.clone()).or_insert(0) += 1;
        }
        if !tickets.is_empty() && files.len() <= 50 {
            for t in tickets {
                let entry = tmap.entry(t.clone()).or_default();
                for f in files {
                    entry.insert(f.clone());
                }
            }
        }
        if files.len() >= 2 && files.len() <= 30 {
            let list: Vec<&String> = files.iter().collect();
            for i in 0..list.len() {
                for j in (i + 1)..list.len() {
                    let key = (list[i].clone(), list[j].clone());
                    *pairs.entry(key).or_insert(0) += 1;
                }
            }
        }
    };

    for line in raw.lines() {
        if let Some(rest) = line.strip_prefix("COMMIT:") {
            flush_commit(
                &current_tickets,
                &current_files,
                &mut file_churn,
                &mut pair_counts,
                &mut ticket_files,
            );
            current_files.clear();
            let subject = rest.split_once('|').map(|(_, s)| s).unwrap_or("");
            current_tickets = extract_ticket_keys(subject);
        } else {
            let norm = normalize_rel_path(line);
            if !norm.is_empty() && is_code_file(&norm) {
                current_files.insert(norm);
            }
        }
    }
    flush_commit(
        &current_tickets,
        &current_files,
        &mut file_churn,
        &mut pair_counts,
        &mut ticket_files,
    );

    let mut co_changes = Vec::new();
    for ((a, b), shared) in pair_counts {
        if shared < 2 {
            continue;
        }
        let ca = *file_churn.get(&a).unwrap_or(&shared);
        let cb = *file_churn.get(&b).unwrap_or(&shared);
        let denom = (ca + cb).saturating_sub(shared);
        if denom == 0 {
            continue;
        }
        let jaccard = (shared as f64) / (denom as f64);
        if jaccard >= 0.30 {
            co_changes.push((a, b, (jaccard * 1000.0).round() / 1000.0));
        }
    }
    co_changes.sort_by(|x, y| y.2.partial_cmp(&x.2).unwrap_or(std::cmp::Ordering::Equal));

    let recent_tickets = ticket_files
        .into_iter()
        .map(|(k, v)| (k, v.into_iter().collect::<Vec<_>>()))
        .collect();

    GitForensics {
        file_churn,
        co_changes,
        recent_tickets,
    }
}

pub fn collect_git_forensics(repo: &Path, since_days: u32) -> GitForensics {
    let since_arg = format!("--since={since_days}.days");
    let output = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args([
            "log",
            &since_arg,
            "--name-only",
            "--pretty=format:COMMIT:%H|%s",
        ])
        .output();

    let Ok(out) = output else {
        return GitForensics::default();
    };
    if !out.status.success() {
        return GitForensics::default();
    }
    let raw = String::from_utf8_lossy(&out.stdout);
    let parsed = parse_git_log_output(&raw);

    // Filter out deleted files no longer present in working tree if needed: keep churn keys normalized
    let mut seen: HashSet<String> = HashSet::new();
    for k in parsed.file_churn.keys() {
        seen.insert(k.clone());
    }
    let _ = seen;
    parsed
}
