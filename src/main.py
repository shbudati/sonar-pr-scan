import os
import sys
import subprocess
import requests
from github import Github
from diff_parser import get_pr_diff, parse_changed_lines

def run_sonar_scanner(host, token, project_key):
    """Runs the sonar-scanner CLI."""
    print("Running SonarScanner...")
    # Architecture Note:
    # SonarQube Community Edition often maintains a single state per project.
    # To avoid reporting unrelated legacy issues on a PR, we rely on the Git Diff.
    # We only report issues that strictly intersect with lines changed in this PR.
    cmd = [
        "sonar-scanner",
        f"-Dsonar.host.url={host}",
        f"-Dsonar.token={token}",
        f"-Dsonar.projectKey={project_key}",
        "-Dsonar.scm.provider=test" # Disable SCM sensor to avoid issues in some docker envs if .git is incomplete
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("SonarScanner failed:")
        print(result.stdout)
        print(result.stderr)
        sys.exit(result.returncode)
    print("SonarScanner finished successfully.")

def get_sonar_issues(host, token, project_key):
    """Fetches open issues from SonarQube."""
    url = f"{host}/api/issues/search"
    params = {
        "componentKeys": project_key,
        "resolved": "false",
        "ps": 500 # Page size
    }
    response = requests.get(url, params=params, auth=(token, ""))
    response.raise_for_status()
    return response.json().get("issues", [])

def get_coverage_metrics(host, token, project_key):
    """Fetches global coverage metrics."""
    url = f"{host}/api/measures/component"
    params = {
        "component": project_key,
        "metricKeys": "coverage,new_coverage"
    }
    metrics = {}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        measures = response.json().get("component", {}).get("measures", [])
        for m in measures:
            metrics[m["metric"]] = m["value"]
    except Exception as e:
        print(f"Warning: Could not fetch global coverage metrics: {e}")
        
    return metrics

def get_file_coverage(host, token, project_key, changed_files):
    """Fetches coverage for changed files."""
    url = f"{host}/api/measures/component_tree"
    params = {
        "component": project_key,
        "metricKeys": "coverage",
        "qualifiers": "FIL",
        "ps": 500
    }
    file_coverage = {}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        components = response.json().get("components", [])
        for comp in components:
            # SonarQube returns 'path' usually matching git path
            path = comp.get('path')
            if path in changed_files:
                for m in comp.get("measures", []):
                    if m["metric"] == "coverage":
                        file_coverage[path] = m["value"]
    except Exception as e:
        print(f"Warning: Could not fetch file coverage: {e}")
    
    return file_coverage

def get_quality_gate_status(host, token, project_key):
    """Fetches the quality gate status of the project."""
    url = f"{host}/api/qualitygates/project_status"
    params = {"projectKey": project_key}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        status = response.json().get("projectStatus", {}).get("status", "UNKNOWN")
        return status
    except Exception as e:
        print(f"Warning: Could not fetch quality gate status: {e}")
        return "UNKNOWN"

def format_comment(issues, changed_lines, metrics, file_coverage, qg_status, host, project_key):
    """Formats the issues into a Markdown comment, filtering by changed lines."""
    relevant_issues = []
    
    for issue in issues:
        component = issue.get("component", "")
        file_path = component.split(":", 1)[-1] if ":" in component else component
        line = issue.get("line")
        
        if file_path in changed_lines and line in changed_lines[file_path]:
            issue_key = issue.get("key")
            link = f"{host}/project/issues?id={project_key}&issues={issue_key}&open={issue_key}"
            
            relevant_issues.append({
                "file": file_path,
                "line": line,
                "message": issue.get("message"),
                "severity": issue.get("severity"),
                "link": link
            })
            
    # Start Building Comment
    comment = "### 🔍 SonarQube Analysis (New Code)\n\n"
    
    # Health / Quality Gate
    status_icons = {
        "OK": "✅ Passed",
        "ERROR": "❌ Failed",
        "WARN": "⚠️ Warning",
        "UNKNOWN": "❓ Unknown"
    }
    health_icon = status_icons.get(qg_status, qg_status)
    comment += f"**PR Health**: {health_icon}\n\n"
    
    # Global Coverage
    if metrics:
        cov = metrics.get('coverage', 'N/A')
        new_cov = metrics.get('new_coverage', 'N/A')
        comment += f"**Overall Coverage**: {cov}%"
        if new_cov != 'N/A':
             comment += f" (New Code: {new_cov}%)"
        comment += "\n\n"

    # File Coverage Table
    if file_coverage:
        comment += "#### 📄 File Coverage\n"
        comment += "| File | Coverage |\n"
        comment += "|------|----------|\n"
        for path, score in file_coverage.items():
            comment += f"| `{path}` | {score}% |\n"
        comment += "\n"

    # Issues Section 
    if not relevant_issues:
        if not metrics and not file_coverage:
             # Nothing to report
            return None 
        if not relevant_issues:
            comment += "✅ No issues found in the new code."
            return comment
        
    comment += "#### 🐛 Issues\n"
    comment += "| Severity | File | Line | Message |\n"
    comment += "|----------|------|------|---------|\n"
    
    icons = {
        "BLOCKER": "🚫",
        "CRITICAL": "🔴",
        "MAJOR": "jg",
        "MINOR": "🟢",
        "INFO": "ℹ️"
    }
    
    for i in relevant_issues:
        icon = icons.get(i['severity'], "")
        message_link = f"[{i['message']}]({i['link']})"
        comment += f"| {icon} {i['severity']} | `{i['file']}` | {i['line']} | {message_link} |\n"
        
    return comment

def main():
    # Inputs
    sonar_host = os.getenv("INPUT_SONAR-HOST-URL")
    sonar_token = os.getenv("INPUT_SONAR-TOKEN")
    project_key = os.getenv("INPUT_PROJECT-KEY")
    github_token = os.getenv("INPUT_GITHUB-TOKEN")
    
    # GitHub Event Info
    event_path = os.getenv("GITHUB_EVENT_PATH")
    repo = os.getenv("GITHUB_REPOSITORY") # owner/repo
    
    # We need the PR number. In a workflow `on: pull_request`, the REF is usually refs/pull/:pr/merge
    # But it's safer to get it from the event payload if available, or GITHUB_REF
    # For simplicity, let's assume we can parse GITHUB_REF or use an input. 
    # Actually, simpler: The Action runs in the context of the PR.
    
    # Verify environment
    if not (sonar_host and sonar_token and project_key and github_token and repo):
        print("Missing required inputs.")
        sys.exit(1)

    # 1. Get PR changes
    # We need PR number.
    # In GitHub Actions, GITHUB_REF for PR is refs/pull/<pr_number>/merge
    ref = os.getenv("GITHUB_REF", "")
    try:
        pr_number = int(ref.split("/")[2])
    except (IndexError, ValueError):
        print("Could not determine PR number from GITHUB_REF. Is this a PR event?")
        sys.exit(1)
        
    print(f"Analyzing PR #{pr_number} for {repo}")
    
    try:
        diff_text = get_pr_diff(repo, pr_number, github_token)
        changed_lines = parse_changed_lines(diff_text)
        print(f"Found changes in {len(changed_lines)} files.")
    except Exception as e:
        print(f"Error parsing diff: {e}")
        sys.exit(1)
        
    # 2. Run Analysis
    run_sonar_scanner(sonar_host, sonar_token, project_key)
    
    # 3. Process Results
    try:
        issues = get_sonar_issues(sonar_host, sonar_token, project_key)
        metrics = get_coverage_metrics(sonar_host, sonar_token, project_key)
        qg_status = get_quality_gate_status(sonar_host, sonar_token, project_key)
        
        # Get list of file paths
        changed_file_paths = list(changed_lines.keys())
        file_coverage = get_file_coverage(sonar_host, sonar_token, project_key, changed_file_paths)
        
        comment_body = format_comment(issues, changed_lines, metrics, file_coverage, qg_status, sonar_host, project_key)
        
        if comment_body:
            print("Posting comment to PR...")
            g = Github(github_token)
            gh_repo = g.get_repo(repo)
            pr = gh_repo.get_pull(pr_number)
            pr.create_issue_comment(comment_body)
        else:
            print("No significant findings to report.")
            
    except Exception as e:
        print(f"Error processing results: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
