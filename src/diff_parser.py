from unidiff import PatchSet
import requests

def get_pr_diff(repo_full_name, pr_number, token):
    """
    Fetches the diff of a PR from GitHub.
    Returns the raw diff content.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff"
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.text

def parse_changed_lines(diff_content):
    """
    Parses a diff and returns a dict mapping file paths to sets of changed line numbers.
    Structure: { 'path/to/file.py': {10, 11, 12, ...} }
    """
    patch = PatchSet(diff_content)
    changed_lines = {}
    
    for patched_file in patch:
        if patched_file.is_binary_file or patched_file.is_removed_file:
            continue
            
        file_path = patched_file.path
        lines = set()
        
        for hunk in patched_file:
            # hunk.target_lines contains the lines in the new version of the file
            for line in hunk.target_lines():
                if line.is_added:
                    lines.add(line.target_line_no)
        
        if lines:
            changed_lines[file_path] = lines
            
    return changed_lines
