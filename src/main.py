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

def get_sonar_data(host, token, project_key, pr_number=None, organization=None, edition='community'):
    """
    Fetches all necessary data from SonarQube.
    If edition is 'community', it uses Global mode (manual line-filtering).
    Otherwise, it attempts PR mode first.
    Returns a tuple: (issues, hotspots, metrics, qg_status, used_pr_mode)
    """
    used_pr_mode = False
    
    # Determine Fetch Strategy
    attempt_pr_mode = edition.lower() in ['cloud', 'developer', 'enterprise'] and pr_number
    
    issues = []
    if attempt_pr_mode:
        try:
            print(f"Attempting PR mode fetch (Edition: {edition})")
            issues = get_sonar_issues(host, token, project_key, pr_number, organization)
            used_pr_mode = True
        except Exception as e:
            print(f"PR mode fetch failed, falling back to global: {e}")
            issues = get_sonar_issues(host, token, project_key, None, organization)
            used_pr_mode = False
    else:
        print(f"Using Community Edition mode (Global API + Manual Filtering)")
        issues = get_sonar_issues(host, token, project_key, None, organization)
        used_pr_mode = False

    print(f"Fetched {len(issues)} issues in {'PR' if used_pr_mode else 'Global'} mode.")

    # Fetch others using the same mode
    fetch_pr = pr_number if used_pr_mode else None
    
    hotspots = get_sonar_hotspots(host, token, project_key, fetch_pr, organization)
    metrics = get_coverage_metrics(host, token, project_key, fetch_pr, organization)
    qg_status = get_quality_gate_status(host, token, project_key, fetch_pr, organization)
    
    return issues, hotspots, metrics, qg_status, used_pr_mode

def get_sonar_issues(host, token, project_key, pr_number=None, organization=None):
    """Fetches open issues from SonarQube."""
    url = f"{host}/api/issues/search"
    params = {
        "componentKeys": project_key,
        "resolved": "false",
        "ps": 500 # Page size
    }
    if pr_number: params["pullRequest"] = pr_number
    if organization: params["organization"] = organization
        
    response = requests.get(url, params=params, auth=(token, ""))
    if response.status_code >= 400:
        print(f"Error fetching issues: {response.status_code} - {response.text}")
    response.raise_for_status()
    return response.json().get("issues", [])

def get_sonar_hotspots(host, token, project_key, pr_number=None, organization=None):
    """Fetches security hotspots from SonarQube."""
    url = f"{host}/api/hotspots/search"
    # Note: /api/hotspots/search uses 'project', while others use 'projectKey' or 'componentKeys'
    params = {
        "project": project_key,
        "ps": 500
    }
    if pr_number: params["pullRequest"] = pr_number
    if organization: params["organization"] = organization
        
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        if response.status_code >= 400:
            print(f"Error fetching hotspots: {response.status_code} - {response.text}")
        response.raise_for_status()
        hotspots = response.json().get("hotspots", [])
        print(f"Fetched {len(hotspots)} hotspots.")
        return hotspots
    except Exception as e:
        print(f"Warning: Could not fetch hotspots: {e}")
        return []

def get_coverage_metrics(host, token, project_key, pr_number=None, organization=None):
    """Fetches coverage metrics."""
    url = f"{host}/api/measures/component"
    params = {
        "component": project_key,
        "metricKeys": "coverage,new_coverage"
    }
    if pr_number: params["pullRequest"] = pr_number
    if organization: params["organization"] = organization
        
    metrics = {}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        if response.status_code >= 400:
            print(f"Error fetching metrics: {response.status_code} - {response.text}")
        response.raise_for_status()
        measures = response.json().get("component", {}).get("measures", [])
        for m in measures:
            metrics[m["metric"]] = m["value"]
    except Exception as e:
        print(f"Warning: Could not fetch metrics: {e}")
        
    return metrics

def get_file_coverage(host, token, project_key, changed_files, pr_number=None, organization=None):
    """Fetches coverage for changed files."""
    url = f"{host}/api/measures/component_tree"
    params = {
        "component": project_key,
        "metricKeys": "coverage",
        "qualifiers": "FIL",
        "ps": 500
    }
    if pr_number: params["pullRequest"] = pr_number
    if organization: params["organization"] = organization
        
    file_coverage = {}
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        if response.status_code >= 400:
            print(f"Error fetching file coverage: {response.status_code} - {response.text}")
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

def get_quality_gate_status(host, token, project_key, pr_number=None, organization=None):
    """Fetches the quality gate status."""
    url = f"{host}/api/qualitygates/project_status"
    params = {"projectKey": project_key}
    if pr_number: params["pullRequest"] = pr_number
    if organization: params["organization"] = organization
        
    try:
        response = requests.get(url, params=params, auth=(token, ""))
        if response.status_code >= 400:
            print(f"Error fetching quality gate status: {response.status_code} - {response.text}")
        response.raise_for_status()
        return response.json().get("projectStatus", {}).get("status", "UNKNOWN")
    except Exception as e:
        print(f"Warning: Could not fetch quality gate status: {e}")
        return "UNKNOWN"

def format_comment(issues, hotspots, changed_lines, metrics, file_coverage, qg_status, host, project_key, pr_number=None, is_pr_mode=False):
    """Formats the findings into a Markdown comment."""
    relevant_issues = []
    print(f"Processing {len(issues)} issues and {len(hotspots)} hotspots. is_pr_mode={is_pr_mode}")
    
    for issue in issues:
        component = issue.get("component", "")
        # Robust path extraction (strip project key if present)
        file_path = component.split(":", 1)[-1] if ":" in component else component
        line = issue.get("line")
        
        # Filtering logic:
        # If is_pr_mode: Trust SonarQube's filter COMPLETELY. 
        # SonarQube already only returns findings for "New Code" when pullRequest param is used.
        if is_pr_mode:
            include = True
        else:
            # Global Mode (Community Edition): Manual Filtering
            # Always include if file is changed and it's a file-level issue (line is None)
            # OR if line strictly matches the diff
            include = file_path in changed_lines and (line is None or line in changed_lines[file_path])
            
        if include:
            issue_key = issue.get("key")
            link = f"{host}/project/issues?id={project_key}&issues={issue_key}&open={issue_key}"
            if pr_number and is_pr_mode: link += f"&pullRequest={pr_number}"
                
            relevant_issues.append({
                "file": file_path,
                "line": line or "N/A",
                "message": issue.get("message"),
                "severity": issue.get("severity"),
                "link": link
            })
        else:
            # Silent filtering for issues (too many logs usually)
            pass

    relevant_hotspots = []
    for hs in hotspots:
        component = hs.get("component", "")
        file_path = component.split(":", 1)[-1] if ":" in component else component
        line = hs.get("line")
        
        if is_pr_mode:
            include = True
        else:
            # Global Mode (Community Edition): ROBUST Filtering for Hotspots
            # Hotspots are critical. We include them if the FILE is modified, 
            # as Sonar's line reporting for hotspots can be imprecise or outside the direct diff chunk.
            include = file_path in changed_lines
            
        if include:
            hs_key = hs.get("key")
            link = f"{host}/security_hotspots?id={project_key}&hotspots={hs_key}"
            if pr_number and is_pr_mode: link += f"&pullRequest={pr_number}"
                
            relevant_hotspots.append({
                "file": file_path,
                "line": line or "N/A",
                "message": hs.get("message"),
                "status": hs.get("status"),
                "link": link
            })
        else:
            print(f"Filtered out hotspot in unchanged file: {file_path}")
            
    # Start Building Comment
    comment = "<!-- sonarppr-scan -->\n"
    comment += "### 🔍 SonarQube Analysis (New Code)\n"
    
    if pr_number and is_pr_mode:
        analysis_link = f"{host}/dashboard?id={project_key}&pullRequest={pr_number}"
        comment += f"[See analysis details on SonarQube]({analysis_link})\n\n"
    else:
        comment += "\n"
    
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
    print(f"Reporting {len(relevant_issues)} relevant issues and {len(relevant_hotspots)} relevant hotspots.")
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
    edition = os.getenv("INPUT_SONAR-EDITION", "community")
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
        issues, hotspots, metrics, qg_status, is_pr_mode = get_sonar_data(sonar_host, sonar_token, project_key, pr_number, organization, edition)
        
        changed_file_paths = list(changed_lines.keys())
        fetch_pr = pr_number if is_pr_mode else None
        file_coverage = get_file_coverage(sonar_host, sonar_token, project_key, changed_file_paths, fetch_pr, organization)
        
        comment_body = format_comment(issues, hotspots, changed_lines, metrics, file_coverage, qg_status, sonar_host, project_key, pr_number, is_pr_mode)
        
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
