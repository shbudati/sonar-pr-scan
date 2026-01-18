# SonarQube Community PR Action

This GitHub Action allows **SonarQube Community Edition** (which typically lacks PR decoration) to comment on Pull Requests.

## 🚀 How it Works

Since Community Edition typically only supports a single branch (stateful), this action uses a **stateless, client-side filtering** approach to ensure PR comments are accurate and isolated, even if the server report is overwritten by other CI jobs.

### 1. The "Override" Problem
In Community Edition, if multiple PRs run analysis against the same project key, the server dashboard will reflect the *most recent* run.
- **Problem**: You cannot rely on the server dashboard to see the history of a specific PR if another PR runs 5 minutes later.
- **Solution**: This action treats the server analysis as a transient calculation engine. It extracts the results immediately and persists them as **GitHub Comments**.

### 2. Identifying "New Code"
To ensure we only report issues caused by *this specific PR*, we do not rely solely on SonarQube's "New Code" definition (which relies on server-side SCM detection and can be flaky with concurrent runs).

Instead, we use **GitHub's Diff API**:

1.  **Fetch Diff**: The action retrieves the raw `.diff` from GitHub for the PR.
2.  **Parse Changes** (`diff_parser.py`): We map exactly which lines in which files were added or modified.
    ```python
    {
       "src/utils.py": {10, 11, 12, ...}
    }
    ```
3.  **Filter Issues**: We fetch *all* current issues from SonarQube. We then iterate through them and validte:
    > Does `issue.file` exist in our map? AND is `issue.line` in the changed set?
    
    - **Match**: report it.
    - **No Match**: Ignore it (it's legacy code).

### 4. Organization Rules (Quality Profiles)
Since this action uses the standard `sonar-scanner` to upload code to **your** SonarQube server, it **automatically respects your Organization's rules**:
-   **Quality Profiles**: The issues returned are based on the ruleset assigned to your project on the server.
-   **Quality Gates**: The "PR Health" status reflects your server-side Quality Gate configuration.

### 5. Coverage Analysis
- **File Coverage**: We report the coverage for any file touched by the PR.
- **New Code Coverage**: We report the global "Coverage on New Code" metric from the specific analysis run.
    - *Note*: As long as analysis runs don't overlap in the exact milliseconds between "Scan Finish" and "Fetch Metrics", this metric is accurate for the PR.

## Usage

See `action.yml` for input definitions.

```yaml
uses: ./path/to/sonar-action
with:
  sonar-host-url: ${{ secrets.SONAR_HOST_URL }}
  sonar-token: ${{ secrets.SONAR_TOKEN }}
  project-key: "my-project"
  github-token: ${{ secrets.GITHUB_TOKEN }}
```

## ❓ Why Docker?
We use a Docker container for this action because **SonarScanner requires Java (OpenJDK 17+)**.
-   Instead of forcing you to install Java and download the Scanner binary in every workflow run, we bundle Python, Java, and the Scanner into a single, ready-to-use image.
-   This ensures consistency and prevents "it works on my machine" issues.
