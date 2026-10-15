import typer
from rich.console import Console
from jobresearcher.db import get_conn, init_db

app = typer.Typer()
console = Console()

@app.command()
def search(query: str, min_score: float = 0.0, no_degree: bool = False):
    """Search job posts in the database."""
    init_db()
    conn = get_conn()
    sql = "SELECT * FROM jobs WHERE match_score >= ? AND (raw_text LIKE ? OR title LIKE ?)"
    params = [min_score, f"%{query}%", f"%{query}%"]
    if no_degree:
        sql += " AND degree_status != 'required'"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    
    if not rows:
        console.print("[yellow]No results found.[/yellow]")
        return
    
    for r in rows:
        console.print(f"[green]{r['title'] or 'N/A'}[/green] — score: {r['match_score']}")
        console.print(f"  {r['raw_text'][:150]}...\n")

@app.command()
def stats():
    """Show database statistics."""
    init_db()
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    console.print(f"Messages: {total} | Processed jobs: {jobs}")
    conn.close()

if __name__ == "__main__":
    app()