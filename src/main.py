import os
import sys
import subprocess
import requests
from github import Github
from diff_parser import get_pr_diff, parse_changed_lines

def run_sonar_scanner(host, token, project_key, organization=None, project_name=None, exclusions=None, binaries=None):
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
        "-Dsonar.scm.disabled=true" # Disable SCM sensor to avoid issues in some docker envs if .git is incomplete
    ]
    if organization:
        cmd.append(f"-Dsonar.organization={organization}")
    if project_name:
        cmd.append(f"-Dsonar.projectName={project_name}")
    if exclusions:
        cmd.append(f"-Dsonar.exclusions={exclusions}")
    if binaries:
        cmd.append(f"-Dsonar.java.binaries={binaries}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("SonarScanner failed:")
        print(result.stdout)
        print(result.stderr)
        sys.exit(result.returncode)
    print("SonarScanner finished successfully.")

def get_sonar_issues(host, token, project_key, pr_number=None):
    """Fetches open issues from SonarQube."""
    url = f"{host}/api/issues/search"
    params = {
        "componentKeys": project_key,
        "resolved": "false",
        "ps": 500 # Page size
    }
    if pr_number:
        params["pullRequest"] = pr_number
        
    response = requests.get(url, params=params, auth=(token, ""))
    response.raise_for_status()
    return response.json().get("issues", [])

def get_sonar_hotspots(host, token, project_key, pr_number=None):
    """Fetches security hotspots from SonarQube."""
    url = f"{host}/api/hotspots/search"
    params = {
        "projectKey": project_key,
        "ps": 500
    }
    if pr_number:
        params["pullRequest"] = pr_number
        
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        return response.json().get("hotspots", [])
    except Exception as e:
        print(f"Warning: Could not fetch security hotspots: {e}")
        return []

def get_coverage_metrics(host, token, project_key, pr_number=None):
    """Fetches coverage metrics."""
    url = f"{host}/api/measures/component"
    params = {
        "component": project_key,
        "metricKeys": "coverage,new_coverage"
    }
    if pr_number:
        params["pullRequest"] = pr_number
        
    metrics = {}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        measures = response.json().get("component", {}).get("measures", [])
        for m in measures:
            metrics[m["metric"]] = m["value"]
    except Exception as e:
        print(f"Warning: Could not fetch coverage metrics: {e}")
        
    return metrics

def get_file_coverage(host, token, project_key, changed_files, pr_number=None):
    """Fetches coverage for changed files."""
    url = f"{host}/api/measures/component_tree"
    params = {
        "component": project_key,
        "metricKeys": "coverage",
        "qualifiers": "FIL",
        "ps": 500
    }
    if pr_number:
        params["pullRequest"] = pr_number
        
    file_coverage = {}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        components = response.json().get("components", [])
        for comp in components:
            path = comp.get('path')
            if path in changed_files:
                for m in comp.get("measures", []):
                    if m["metric"] == "coverage":
                        file_coverage[path] = m["value"]
    except Exception as e:
        print(f"Warning: Could not fetch file coverage: {e}")
    
    return file_coverage

def get_quality_gate_status(host, token, project_key, pr_number=None):
    """Fetches the quality gate status."""
    url = f"{host}/api/qualitygates/project_status"
    params = {"projectKey": project_key}
    if pr_number:
        params["pullRequest"] = pr_number
        
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        response.raise_for_status()
        status = response.json().get("projectStatus", {}).get("status", "UNKNOWN")
        return status
    except Exception as e:
        print(f"Warning: Could not fetch quality gate status: {e}")
        return "UNKNOWN"

def format_comment(issues, hotspots, changed_lines, metrics, file_coverage, qg_status, host, project_key):
    """Formats the findings into a Markdown comment."""
    relevant_issues = []
    for issue in issues:
        component = issue.get("component", "")
        file_path = component.split(":", 1)[-1] if ":" in component else component
        line = issue.get("line")
        
        # If we have line info, filter by changed lines. If not (e.g. file-level issue), include if file changed.
        if file_path in changed_lines and (line is None or line in changed_lines[file_path]):
            issue_key = issue.get("key")
            link = f"{host}/project/issues?id={project_key}&issues={issue_key}&open={issue_key}"
            relevant_issues.append({
                "file": file_path,
                "line": line or "N/A",
                "message": issue.get("message"),
                "severity": issue.get("severity"),
                "link": link
            })

    relevant_hotspots = []
    for hs in hotspots:
        component = hs.get("component", "")
        file_path = component.split(":", 1)[-1] if ":" in component else component
        line = hs.get("line")
        if file_path in changed_lines and (line is None or line in changed_lines[file_path]):
            hs_key = hs.get("key")
            link = f"{host}/security_hotspots?id={project_key}&hotspots={hs_key}"
            relevant_hotspots.append({
                "file": file_path,
                "line": line or "N/A",
                "message": hs.get("message"),
                "status": hs.get("status"),
                "link": link
            })
            
    # Start Building Comment
    comment = "<!-- sonarppr-scan -->\n"
    comment += "### 🔍 SonarQube Analysis (New Code)\n\n"
    
    status_icons = {
        "OK": "✅ Passed",
        "ERROR": "❌ Failed",
        "WARN": "⚠️ Warning",
        "UNKNOWN": "❓ Unknown"
    }
    health_icon = status_icons.get(qg_status, qg_status)
    comment += f"**PR Health**: {health_icon}\n\n"
    
    if metrics:
        cov = metrics.get('coverage', 'N/A')
        new_cov = metrics.get('new_coverage', 'N/A')
        comment += f"**Overall Coverage**: {cov}%"
        if new_cov != 'N/A':
             comment += f" (New Code: {new_cov}%)"
        comment += "\n\n"

    if file_coverage:
        comment += "#### 📄 File Coverage\n"
        comment += "| File | Coverage |\n"
        comment += "|------|----------|\n"
        for path, score in file_coverage.items():
            comment += f"| `{path}` | {score}% |\n"
        comment += "\n"

    # Issues Section 
    if not relevant_issues and not relevant_hotspots:
        comment += "✅ No issues found in the new code."
    else:
        if relevant_issues:
            comment += "#### 🐛 Issues\n"
            comment += "| Severity | File | Line | Message |\n"
            comment += "|----------|------|------|---------|\n"
            icons = {"BLOCKER": "🚫", "CRITICAL": "🔴", "MAJOR": "🟠", "MINOR": "🟢", "INFO": "ℹ️"}
            for i in relevant_issues:
                icon = icons.get(i['severity'], "️")
                comment += f"| {icon} {i['severity']} | `{i['file']}` | {i['line']} | [{i['message']}]({i['link']}) |\n"
            comment += "\n"

        if relevant_hotspots:
            comment += "#### 🛡️ Security Hotspots\n"
            comment += "| Status | File | Line | Message |\n"
            comment += "|--------|------|------|---------|\n"
            for hs in relevant_hotspots:
                comment += f"| 🛡️ {hs['status']} | `{hs['file']}` | {hs['line']} | [{hs['message']}]({hs['link']}) |\n"
            comment += "\n"
        
    comment += "\n---\n"
    comment += "_Reported by sonarppr-scan_"
    return comment

def main():
    # Inputs
    sonar_host = os.getenv("INPUT_SONAR-HOST-URL")
    sonar_token = os.getenv("INPUT_SONAR-TOKEN")
    project_key = os.getenv("INPUT_PROJECT-KEY")
    project_name = os.getenv("INPUT_PROJECT-NAME")
    organization = os.getenv("INPUT_SONAR-ORGANIZATION")
    exclusions = os.getenv("INPUT_EXCLUSIONS")
    binaries = os.getenv("INPUT_BINARIES")
    github_token = os.getenv("INPUT_GITHUB-TOKEN")
    
    repo = os.getenv("GITHUB_REPOSITORY")
    ref = os.getenv("GITHUB_REF", "")
    try:
        pr_number = int(ref.split("/")[2])
    except (IndexError, ValueError):
        print("Could not determine PR number from GITHUB_REF. Is this a PR event?")
        sys.exit(1)

    # 1. Get PR changes
    try:
        diff_text = get_pr_diff(repo, pr_number, github_token)
        changed_lines = parse_changed_lines(diff_text)
        print(f"Found changes in {len(changed_lines)} files.")
    except Exception as e:
        print(f"Error parsing diff: {e}")
        sys.exit(1)
        
    # 2. Run Analysis
    run_sonar_scanner(sonar_host, sonar_token, project_key, organization, project_name, exclusions, binaries)
    
    # 3. Process Results
    try:
        issues = get_sonar_issues(sonar_host, sonar_token, project_key, pr_number)
        hotspots = get_sonar_hotspots(sonar_host, sonar_token, project_key, pr_number)
        metrics = get_coverage_metrics(sonar_host, sonar_token, project_key, pr_number)
        qg_status = get_quality_gate_status(sonar_host, sonar_token, project_key, pr_number)
        
        changed_file_paths = list(changed_lines.keys())
        file_coverage = get_file_coverage(sonar_host, sonar_token, project_key, changed_file_paths, pr_number)
        
        comment_body = format_comment(issues, hotspots, changed_lines, metrics, file_coverage, qg_status, sonar_host, project_key)
        
        if comment_body:
            print("Posting comment to PR...")
            g = Github(github_token)
            gh_repo = g.get_repo(repo)
            pr = gh_repo.get_pull(pr_number)
            pr.create_issue_comment(comment_body)
    except Exception as e:
        print(f"Error processing results: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
