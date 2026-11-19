use anyhow::Result;
use clap::{Parser, Subcommand};
use radar_core::{chunker, git_forensics, schema, scip_graph};
use std::fs;
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    name = "radar-core",
    about = "Polyglot Pre-Refactor Extractor (Rust / Java / Go)"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Phase 1: Scan a microservice repo and write Parquet capsule artifacts
    Extract {
        #[arg(long)]
        repo: PathBuf,
        #[arg(long)]
        service_name: String,
        #[arg(long)]
        capsule_out: PathBuf,
        #[arg(long)]
        scip_index: Option<PathBuf>,
        #[arg(long, default_value_t = 90)]
        since_days: u32,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Extract {
            repo,
            service_name,
            capsule_out,
            scip_index,
            since_days,
        } => {
            fs::create_dir_all(&capsule_out)?;
            let forensics = git_forensics::collect_git_forensics(&repo, since_days);
            let mut nodes =
                chunker::extract_ast_nodes(&repo, &service_name, &forensics.file_churn)?;
            let mut edges = if let Some(scip_path) = scip_index {
                scip_graph::enrich_with_scip(&scip_path, &mut nodes)?
            } else {
                vec![]
            };
            scip_graph::attach_co_change_edges(&forensics.co_changes, &nodes, &mut edges);

            schema::write_nodes_parquet(&nodes, &capsule_out.join("nodes.parquet"))?;
            schema::write_edges_parquet(&edges, &capsule_out.join("edges.parquet"))?;
            let tickets_json = serde_json::to_string_pretty(&forensics)?;
            fs::write(capsule_out.join("git_tickets.json"), tickets_json)?;

            println!(
                "[radar-core] Capsule ready: {} symbols, {} edges, {} co-change pairs -> {}",
                nodes.len(),
                edges.len(),
                forensics.co_changes.len(),
                capsule_out.display()
            );
        }
    }
    Ok(())
}

