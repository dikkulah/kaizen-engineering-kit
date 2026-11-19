use protobuf::Message;
use radar_core::{chunker, git_forensics, schema, scip_graph};
use scip::types::{Document, Index, Occurrence, SymbolInformation, SymbolRole};
use std::collections::HashMap;
use std::fs;
use tempfile::TempDir;

#[test]
fn extracts_rust_java_go_callables_with_cognitive_complexity() {
    let dir = TempDir::new().unwrap();
    let src_dir = dir.path().join("src");
    fs::create_dir_all(&src_dir).unwrap();

    fs::write(
        src_dir.join("order.rs"),
        r#"
struct OrderService;
impl OrderService {
    // comment line should be stripped
    pub fn validate(&self, amount: i64, vip: bool) -> bool {
        if amount <= 0 {
            return false;
        }
        if vip {
            for i in 0..3 {
                if i == 2 {
                    return true;
                }
            }
        }
        true
    }
}
"#,
    )
    .unwrap();

    fs::write(
        src_dir.join("BillingController.java"),
        r#"
public class BillingController {
    public String invoice(int tier, boolean active) {
        if (!active) {
            return "disabled";
        }
        switch (tier) {
            case 1:
                return "basic";
            default:
                return "pro";
        }
    }
}
"#,
    )
    .unwrap();

    fs::write(
        src_dir.join("handler.go"),
        r#"
package main

type Handler struct{}

func (h *Handler) Serve(retry bool) int {
    if retry {
        for i := 0; i < 3; i++ {
            if i == 1 {
                return 200
            }
        }
    }
    return 500
}
"#,
    )
    .unwrap();

    let mut churn = HashMap::new();
    churn.insert("src/order.rs".to_string(), 7);
    churn.insert("src/BillingController.java".to_string(), 3);
    churn.insert("src/handler.go".to_string(), 5);

    let nodes = chunker::extract_ast_nodes(dir.path(), "poly-svc", &churn).unwrap();
    assert_eq!(nodes.len(), 3);

    let rs = nodes.iter().find(|n| n.language == "rust").unwrap();
    assert_eq!(rs.name, "OrderService::validate");
    assert_eq!(rs.file_path, "src/order.rs");
    assert!(!rs.clean_body.contains("comment line"));
    assert!(rs.complexity >= 6);
    assert!(rs.hotspot_score > 0.0);

    let jv = nodes.iter().find(|n| n.language == "java").unwrap();
    assert_eq!(jv.name, "BillingController.invoice");
    assert_eq!(jv.file_path, "src/BillingController.java");
    assert!(jv.complexity >= 3);

    let go = nodes.iter().find(|n| n.language == "go").unwrap();
    assert_eq!(go.name, "Handler.Serve");
    assert_eq!(go.file_path, "src/handler.go");
    assert!(go.complexity >= 5);
}

#[test]
fn parses_git_forensics_windows_paths_co_change_and_tickets() {
    let raw_log = "\
COMMIT:aaa111|UC-1042 fix retry loop
src\\order.rs
src\\BillingController.java

COMMIT:bbb222|UC-1042 update validation
src/order.rs
src/BillingController.java

COMMIT:ccc333|UC-999 unrelated
src/handler.go
";
    let forensics = git_forensics::parse_git_log_output(raw_log);
    assert_eq!(forensics.file_churn.get("src/order.rs"), Some(&2));
    assert_eq!(forensics.file_churn.get("src/BillingController.java"), Some(&2));
    assert_eq!(forensics.co_changes.len(), 1);
    assert!((forensics.co_changes[0].2 - 1.0).abs() < 1e-6);
    assert!(forensics.recent_tickets.contains_key("UC-1042"));
    assert_eq!(forensics.recent_tickets["UC-1042"].len(), 2);
}

#[test]
fn enriches_scip_and_writes_zero_and_nonempty_parquet() {
    let dir = TempDir::new().unwrap();
    let src_dir = dir.path().join("src");
    fs::create_dir_all(&src_dir).unwrap();

    fs::write(
        src_dir.join("svc.rs"),
        r#"
pub fn caller() -> i32 {
    let a = 1;
    let b = callee();
    a + b
}

pub fn callee() -> i32 {
    let x = 10;
    let y = 20;
    x + y
}
"#,
    )
    .unwrap();

    let mut nodes = chunker::extract_ast_nodes(dir.path(), "svc", &HashMap::new()).unwrap();
    assert_eq!(nodes.len(), 2);

    // Write 0-edge parquet cleanly
    let empty_edges_path = dir.path().join("empty_edges.parquet");
    schema::write_edges_parquet(&[], &empty_edges_path).unwrap();
    assert!(empty_edges_path.exists());

    // Build synthetic SCIP index with Windows backslash relative_path
    let mut doc = Document::new();
    doc.relative_path = "src\\svc.rs".to_string();

    let mut occ_def_caller = Occurrence::new();
    occ_def_caller.range = vec![1, 7, 13];
    occ_def_caller.symbol = "rust-analyzer cargo svc 0.1.0 caller().".to_string();
    occ_def_caller.symbol_roles = SymbolRole::Definition as i32;

    let mut occ_def_callee = Occurrence::new();
    occ_def_callee.range = vec![7, 7, 13];
    occ_def_callee.symbol = "rust-analyzer cargo svc 0.1.0 callee().".to_string();
    occ_def_callee.symbol_roles = SymbolRole::Definition as i32;

    let mut occ_ref_callee = Occurrence::new();
    occ_ref_callee.range = vec![3, 12, 18];
    occ_ref_callee.symbol = "rust-analyzer cargo svc 0.1.0 callee().".to_string();
    occ_ref_callee.symbol_roles = SymbolRole::ReadAccess as i32;

    doc.occurrences = vec![occ_def_caller, occ_def_callee, occ_ref_callee];
    let mut sym_info = SymbolInformation::new();
    sym_info.symbol = "rust-analyzer cargo svc 0.1.0 caller().".to_string();
    doc.symbols = vec![sym_info];

    let mut idx = Index::new();
    idx.documents = vec![doc];
    let scip_path = dir.path().join("index.scip");
    let bytes = idx.write_to_bytes().unwrap();
    fs::write(&scip_path, bytes).unwrap();

    let edges = scip_graph::enrich_with_scip(&scip_path, &mut nodes).unwrap();
    assert_eq!(edges.len(), 1);
    assert_eq!(edges[0].edge_type, "CALLS");

    let caller_node = nodes.iter().find(|n| n.name == "caller").unwrap();
    let callee_node = nodes.iter().find(|n| n.name == "callee").unwrap();
    assert_eq!(caller_node.fan_out, 1);
    assert_eq!(callee_node.fan_in, 1);

    let nodes_pq = dir.path().join("nodes.parquet");
    schema::write_nodes_parquet(&nodes, &nodes_pq).unwrap();
    assert!(nodes_pq.metadata().unwrap().len() > 0);
}

