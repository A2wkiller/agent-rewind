#!/usr/bin/env python3
"""One-click helper to initialize and push TraceGate to your GitHub repo."""
import subprocess
import sys
def run_cmd(cmd: list[str]) -> str:
   res = subprocess.run(cmd, capture_output=True, text=True)
   if res.returncode != 0:
       print(f"Error running {' '.join(cmd)}:\n{res.stderr.strip()}")
       sys.exit(res.returncode)
   return res.stdout.strip()
def main() -> None:
   print("🚀 Initializing and staging TraceGate files...")
   run_cmd(["git", "init", "-b", "main"])
   run_cmd(["git", "add", "."])
   # Check if there is anything to commit
   status = run_cmd(["git", "status", "--porcelain"])
   if status:
       run_cmd(
           [
               "git",
               "commit",
               "-m",
               "feat: initial commit for TraceGate execution proxy, token firewall & universal backtracker",
           ]
       )
       print("✅ Committed all project files.")
   else:
       print("ℹ️ Working directory clean, no new files to commit.")
   # Check remote
   remotes = run_cmd(["git", "remote"])
   if "origin" not in remotes.split():
       repo_url = input("\nPaste your GitHub Repository URL (HTTPS or SSH): ").strip()
       if not repo_url:
           print("❌ Repository URL cannot be empty.")
           sys.exit(1)
       run_cmd(["git", "remote", "add", "origin", repo_url])
       print(f"🔗 Linked remote origin -> {repo_url}")
   print("📤 Pushing to GitHub (main)...")
   subprocess.run(["git", "push", "-u", "origin", "main"], check=True)
   print("\n🎉 Repository pushed successfully! TraceGate is live on GitHub.")
if __name__ == "__main__":
   main()
