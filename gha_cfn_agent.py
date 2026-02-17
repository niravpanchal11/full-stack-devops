#!/usr/bin/env python3
"""
GitHub Actions Auto-Fixer with LLM
Analyzes GHA failures, fixes issues, and creates PRs automatically
"""

import os
import sys
import json
import zipfile
import tempfile
import requests
from pathlib import Path
from typing import Dict, List, Optional
import google.generativeai as genai
from github import Github
from github.GithubException import GithubException


class GHAAutoFixer:
    def __init__(self, github_token: str, gemini_api_key: str, model: str = "gemini-1.5-pro-latest"):
        """Initialize the auto-fixer with API credentials"""
        self.github_token = github_token
        self.gh = Github(github_token)
        self.gemini_api_key = gemini_api_key
        self.model = model
        
        # Configure Gemini
        genai.configure(api_key=gemini_api_key)
        
    def download_logs(self, owner: str, repo: str, run_id: str) -> str:
        """Download GitHub Actions logs"""
        print(f"📥 Downloading logs for run {run_id}...")
        
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.get(url, headers=headers, allow_redirects=True)
        
        if response.status_code != 200:
            raise Exception(f"Failed to download logs: {response.status_code} - {response.text}")
        
        # Save to temporary file
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, "logs.zip")
        
        with open(zip_path, 'wb') as f:
            f.write(response.content)
        
        # Extract logs
        extract_dir = os.path.join(temp_dir, "logs")
        os.makedirs(extract_dir, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        
        print(f"✅ Logs downloaded and extracted to {extract_dir}")
        return extract_dir
    
    def read_log_files(self, log_dir: str) -> Dict[str, str]:
        """Read all log files from the extracted directory"""
        logs = {}
        log_path = Path(log_dir)
        
        for log_file in log_path.rglob("*.txt"):
            relative_path = log_file.relative_to(log_path)
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                logs[str(relative_path)] = f.read()
        
        return logs
    
    def get_workflow_info(self, owner: str, repo: str, run_id: str) -> Dict:
        """Get workflow run information"""
        print(f"📋 Fetching workflow information...")
        
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get workflow info: {response.status_code}")
        
        return response.json()
    
    def get_repository_structure(self, owner: str, repo: str, branch: str = "main") -> Dict:
        """Get repository structure and important files"""
        print(f"📂 Analyzing repository structure...")
        
        repository = self.gh.get_repo(f"{owner}/{repo}")
        
        structure = {
            "workflows": [],
            "source_files": [],
            "config_files": [],
            "readme": None
        }
        
        try:
            # Get workflow files
            workflows_contents = repository.get_contents(".github/workflows", ref=branch)
            for content in workflows_contents:
                if content.type == "file" and content.name.endswith(('.yml', '.yaml')):
                    structure["workflows"].append({
                        "name": content.name,
                        "path": content.path,
                        "content": content.decoded_content.decode('utf-8')
                    })
        except GithubException:
            print("⚠️  No workflows found or unable to access .github/workflows")
        
        # Get main source files
        try:
            contents = repository.get_contents("", ref=branch)
            for content in contents:
                if content.type == "file":
                    if content.name.endswith(('.py', '.js', '.ts', '.java', '.go', '.rb')):
                        try:
                            structure["source_files"].append({
                                "name": content.name,
                                "path": content.path,
                                "content": content.decoded_content.decode('utf-8')
                            })
                        except:
                            pass
                    elif content.name in ['package.json', 'requirements.txt', 'Gemfile', 
                                         'go.mod', 'pom.xml', 'build.gradle', 'Cargo.toml',
                                         'pyproject.toml', 'setup.py']:
                        try:
                            structure["config_files"].append({
                                "name": content.name,
                                "path": content.path,
                                "content": content.decoded_content.decode('utf-8')
                            })
                        except:
                            pass
                    elif content.name.upper() == 'README.MD':
                        try:
                            structure["readme"] = content.decoded_content.decode('utf-8')
                        except:
                            pass
        except GithubException as e:
            print(f"⚠️  Error accessing repository contents: {e}")
        
        return structure
    
    def list_available_models(self) -> List[str]:
        """List all available Gemini models"""
        try:
            models = genai.list_models()
            available = []
            for model in models:
                if 'generateContent' in model.supported_generation_methods:
                    available.append(model.name.replace('models/', ''))
            return available
        except Exception as e:
            print(f"⚠️  Could not list models: {e}")
            return []
    
    def clean_json_content(self, content: str) -> str:
        """Clean and unescape content from JSON"""
        if not content:
            return content
        
        # Replace literal \n with actual newlines
        content = content.replace('\\n', '\n')
        # Unescape quotes
        content = content.replace('\\"', '"')
        # Unescape backslashes
        content = content.replace('\\\\', '\\')
        
        return content
    
    def analyze_with_llm(self, logs: Dict[str, str], workflow_info: Dict, 
                         repo_structure: Dict) -> Dict:
        """Analyze logs using Google Gemini AI to understand the failure and suggest fixes"""
        print(f"🤖 Analyzing failure with Google Gemini AI ({self.model})...")
        
        # Prepare context for the LLM
        logs_summary = "\n\n".join([f"=== {name} ===\n{content[-5000:]}" 
                                    for name, content in logs.items()])
        
        workflows_summary = "\n\n".join([
            f"=== Workflow: {wf['name']} ===\n{wf['content']}"
            for wf in repo_structure.get('workflows', [])
        ])
        
        config_files_summary = "\n\n".join([
            f"=== Config: {cf['name']} ===\n{cf['content']}"
            for cf in repo_structure.get('config_files', [])
        ])
        
        source_files_summary = "\n\n".join([
            f"=== Source: {sf['name']} ===\n{sf['content'][:2000]}"
            for sf in repo_structure.get('source_files', [])[:5]  # Limit to first 5 files
        ])
        
        prompt = f"""You are an expert DevOps engineer analyzing a GitHub Actions workflow failure.

**Workflow Information:**
- Workflow: {workflow_info.get('name', 'Unknown')}
- Run ID: {workflow_info.get('id', 'Unknown')}
- Status: {workflow_info.get('conclusion', 'Unknown')}
- Branch: {workflow_info.get('head_branch', 'Unknown')}

**Repository Structure:**
{repo_structure.get('readme', 'No README available')[:1000]}

**Workflow Files:**
{workflows_summary}

**Configuration Files:**
{config_files_summary}

**Source Files (sample):**
{source_files_summary}

**Failure Logs (last 5000 chars of each):**
{logs_summary}

**Your Task:**
1. Analyze the logs to identify the root cause of the failure
2. Examine the workflow files and repository structure
3. Identify ALL files that need to be changed or created
4. Provide specific, complete fixes for each file

**CRITICAL: Response Format**
You MUST return ONLY valid JSON with NO additional text, markdown, or formatting.
Do NOT use line breaks within string values. Use \\n for line breaks.
Do NOT include any text before or after the JSON.

Return this exact JSON structure:
{{
  "analysis": "Single line explanation of what went wrong",
  "root_cause": "Primary cause in one sentence",
  "files_to_change": [
    {{
      "path": "path/to/file",
      "action": "modify",
      "reason": "Why this change is needed",
      "content": "Complete file content with \\n for line breaks"
    }}
  ],
  "pr_title": "Brief PR title",
  "pr_description": "PR description with \\n for line breaks"
}}

IMPORTANT: 
- Use \\n instead of actual line breaks in content
- Escape all quotes inside strings with \\"
- Keep analysis and root_cause as single lines
- The JSON must be parseable by json.loads()
- Return ONLY the JSON, nothing else"""

        # List of models to try in order of preference
        models_to_try = [
            self.model,
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash-latest",
            "gemini-pro",
            "gemini-1.5-flash",
            "gemini-1.5-pro"
        ]
        
        # Remove duplicates while preserving order
        seen = set()
        models_to_try = [m for m in models_to_try if not (m in seen or seen.add(m))]
        
        last_error = None
        
        for model_name in models_to_try:
            try:
                print(f"   Trying model: {model_name}...")
                
                # Create Gemini model instance with generation config
                generation_config = {
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "top_k": 40,
                    "max_output_tokens": 8192,
                }
                
                model = genai.GenerativeModel(
                    model_name=model_name,
                    generation_config=generation_config
                )
                
                # Generate response
                response = model.generate_content(prompt)
                response_text = response.text
                
                print(f"   ✅ Successfully used model: {model_name}")
                
                # Extract and clean JSON from response
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                
                if json_start >= 0 and json_end > json_start:
                    json_str = response_text[json_start:json_end]
                    
                    # Try to parse JSON with multiple strategies
                    analysis_result = None
                    
                    # Strategy 1: Parse as-is
                    try:
                        analysis_result = json.loads(json_str)
                    except json.JSONDecodeError as e:
                        print(f"   ⚠️  JSON parse error (attempt 1): {e}")
                        
                        # Strategy 2: Try to fix common issues
                        try:
                            # Remove markdown code blocks if present
                            cleaned = json_str.replace('```json', '').replace('```', '')
                            # Remove any BOM or whitespace
                            cleaned = cleaned.strip()
                            analysis_result = json.loads(cleaned)
                            print(f"   ✅ Parsed JSON after cleaning")
                        except json.JSONDecodeError as e2:
                            print(f"   ⚠️  JSON parse error (attempt 2): {e2}")
                            
                            # Strategy 3: Extract just the essential parts using regex
                            try:
                                import re
                                # Try to extract individual fields
                                root_cause_match = re.search(r'"root_cause"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', json_str)
                                analysis_match = re.search(r'"analysis"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', json_str)
                                pr_title_match = re.search(r'"pr_title"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', json_str)
                                
                                if root_cause_match and analysis_match:
                                    print(f"   ✅ Extracted fields using regex")
                                    analysis_result = {
                                        "analysis": analysis_match.group(1) if analysis_match else response_text[:500],
                                        "root_cause": root_cause_match.group(1) if root_cause_match else "JSON parsing error",
                                        "files_to_change": [],
                                        "pr_title": pr_title_match.group(1) if pr_title_match else "Fix GitHub Actions workflow failure",
                                        "pr_description": f"AI Analysis:\n\n{response_text[:1000]}\n\n⚠️ Note: Could not parse complete structured response. Manual review recommended."
                                    }
                            except Exception as e3:
                                print(f"   ⚠️  Regex extraction error: {e3}")
                    
                    # If all parsing failed, create a fallback response
                    if analysis_result is None:
                        print(f"   ⚠️  All JSON parsing strategies failed, using fallback")
                        # Save raw response for debugging
                        with open('/tmp/gemini_response_debug.txt', 'w') as f:
                            f.write(response_text)
                        print(f"   💾 Raw response saved to /tmp/gemini_response_debug.txt")
                        
                        analysis_result = {
                            "analysis": response_text[:500] + "...",
                            "root_cause": "Unable to parse AI response - check /tmp/gemini_response_debug.txt",
                            "files_to_change": [],
                            "pr_title": "Fix GitHub Actions workflow failure",
                            "pr_description": f"⚠️ AI analysis completed but response parsing failed.\n\nRaw response excerpt:\n{response_text[:1000]}\n\nFull response saved to /tmp/gemini_response_debug.txt"
                        }
                else:
                    print(f"   ⚠️  No JSON found in response")
                    analysis_result = {
                        "analysis": response_text,
                        "root_cause": "No structured response found",
                        "files_to_change": [],
                        "pr_title": "Fix GitHub Actions workflow failure",
                        "pr_description": response_text[:1000] if len(response_text) > 1000 else response_text
                    }
                
                print(f"✅ Analysis complete!")
                print(f"\n📊 Root Cause: {analysis_result.get('root_cause', 'Unknown')}")
                print(f"📝 Files to change: {len(analysis_result.get('files_to_change', []))}")
                
                # Clean file content (unescape \n, etc.)
                for file_change in analysis_result.get('files_to_change', []):
                    if 'content' in file_change and file_change['content']:
                        file_change['content'] = self.clean_json_content(file_change['content'])
                
                return analysis_result
                
            except Exception as e:
                last_error = e
                if "not found" in str(e).lower() or "404" in str(e):
                    print(f"   ⚠️  Model {model_name} not available, trying next...")
                    continue
                else:
                    # For other errors, fail immediately
                    print(f"   ❌ Error with model {model_name}: {e}")
                    raise
        
        # If we get here, all models failed
        print(f"\n❌ All models failed. Last error: {last_error}")
        print(f"\n💡 Available models:")
        available = self.list_available_models()
        if available:
            for model in available[:10]:  # Show first 10
                print(f"   - {model}")
            print(f"\n   Set GEMINI_MODEL to one of the above models and try again.")
        else:
            print(f"   Could not retrieve available models.")
            print(f"   Try: gemini-1.5-pro-latest or gemini-1.5-flash-latest")
        
        raise Exception(f"Failed to get response from any Gemini model. Last error: {last_error}")
    
    def create_branch(self, owner: str, repo: str, base_branch: str, 
                     new_branch: str) -> None:
        """Create a new branch from base branch"""
        print(f"🌿 Creating branch '{new_branch}'...")
        
        repository = self.gh.get_repo(f"{owner}/{repo}")
        
        # Get the base branch reference
        try:
            base_ref = repository.get_git_ref(f"heads/{base_branch}")
            base_sha = base_ref.object.sha
        except GithubException as e:
            if e.status == 403:
                print(f"\n❌ ERROR: GitHub token doesn't have permission to read repository")
                print(f"   Your token needs 'repo' scope (full control of private repositories)")
                print(f"   OR 'public_repo' scope (for public repositories only)")
                print(f"\n📋 How to fix:")
                print(f"   1. Go to: https://github.com/settings/tokens")
                print(f"   2. Click on your token or create a new one")
                print(f"   3. Ensure these scopes are checked:")
                print(f"      ✅ repo (Full control - recommended)")
                print(f"      ✅ workflow (Update workflows)")
                print(f"   4. Save changes and update your GITHUB_TOKEN")
                raise
            else:
                raise
        
        # Create new branch
        try:
            repository.create_git_ref(f"refs/heads/{new_branch}", base_sha)
            print(f"✅ Branch '{new_branch}' created successfully")
        except GithubException as e:
            if e.status == 422:
                print(f"⚠️  Branch '{new_branch}' already exists, will use existing branch")
            elif e.status == 403:
                print(f"\n❌ ERROR: GitHub token doesn't have permission to create branches")
                print(f"   This is a PERMISSIONS issue, not an authentication issue.")
                print(f"\n🔍 Possible causes:")
                print(f"   1. Your token is missing required scopes")
                print(f"   2. You're using a fine-grained token with insufficient permissions")
                print(f"   3. Repository is in an organization with restrictions")
                print(f"\n📋 How to fix:")
                print(f"   1. Go to: https://github.com/settings/tokens")
                print(f"   2. Delete your current token")
                print(f"   3. Create a NEW 'Personal access token (classic)'")
                print(f"   4. Select these scopes:")
                print(f"      ✅ repo (Full control of private repositories)")
                print(f"      ✅ workflow (Update GitHub Action workflows)")
                print(f"   5. Generate token and copy it")
                print(f"   6. Update: export GITHUB_TOKEN='your_new_token'")
                print(f"\n⚠️  IMPORTANT: Use 'classic' token, NOT 'fine-grained'")
                print(f"   Fine-grained tokens have more restrictions.")
                raise
            else:
                raise
    
    def apply_fixes(self, owner: str, repo: str, branch: str, 
                    files_to_change: List[Dict]) -> None:
        """Apply the fixes to the repository"""
        print(f"🔧 Applying fixes to branch '{branch}'...")
        
        repository = self.gh.get_repo(f"{owner}/{repo}")
        
        for file_change in files_to_change:
            path = file_change['path']
            action = file_change['action']
            content = file_change.get('content', '')
            
            print(f"  📝 {action.upper()}: {path}")
            
            try:
                if action == "modify":
                    # Get current file to obtain its SHA
                    file_content = repository.get_contents(path, ref=branch)
                    repository.update_file(
                        path=path,
                        message=f"Fix: {file_change.get('reason', 'Update file')}",
                        content=content,
                        sha=file_content.sha,
                        branch=branch
                    )
                elif action == "create":
                    repository.create_file(
                        path=path,
                        message=f"Create: {file_change.get('reason', 'Create file')}",
                        content=content,
                        branch=branch
                    )
                elif action == "delete":
                    file_content = repository.get_contents(path, ref=branch)
                    repository.delete_file(
                        path=path,
                        message=f"Delete: {file_change.get('reason', 'Delete file')}",
                        sha=file_content.sha,
                        branch=branch
                    )
                
                print(f"  ✅ {path} updated successfully")
                
            except GithubException as e:
                print(f"  ❌ Error updating {path}: {e}")
                if action == "modify" and e.status == 404:
                    print(f"  ℹ️  File not found, attempting to create instead...")
                    try:
                        repository.create_file(
                            path=path,
                            message=f"Create: {file_change.get('reason', 'Create file')}",
                            content=content,
                            branch=branch
                        )
                        print(f"  ✅ {path} created successfully")
                    except Exception as e2:
                        print(f"  ❌ Failed to create {path}: {e2}")
    
    def create_pull_request(self, owner: str, repo: str, head_branch: str,
                           base_branch: str, title: str, description: str) -> str:
        """Create a pull request"""
        print(f"🔀 Creating pull request...")
        
        repository = self.gh.get_repo(f"{owner}/{repo}")
        
        try:
            pr = repository.create_pull(
                title=title,
                body=description,
                head=head_branch,
                base=base_branch
            )
            
            print(f"✅ Pull request created: {pr.html_url}")
            return pr.html_url
            
        except GithubException as e:
            print(f"❌ Error creating pull request: {e}")
            raise
    
    def fix_workflow(self, run_url: str) -> str:
        """Main method to fix a workflow from a run URL"""
        print(f"🚀 Starting auto-fix process for: {run_url}\n")
        
        # Parse URL
        # Format: https://github.com/OWNER/REPO/actions/runs/RUN_ID
        parts = run_url.replace("https://github.com/", "").split("/")
        owner = parts[0]
        repo = parts[1]
        run_id = parts[4]
        
        print(f"📍 Repository: {owner}/{repo}")
        print(f"📍 Run ID: {run_id}\n")
        
        # Step 1: Get workflow information
        workflow_info = self.get_workflow_info(owner, repo, run_id)
        base_branch = workflow_info.get('head_branch', 'main')
        
        # Step 2: Download and read logs
        log_dir = self.download_logs(owner, repo, run_id)
        logs = self.read_log_files(log_dir)
        
        # Step 3: Get repository structure
        repo_structure = self.get_repository_structure(owner, repo, base_branch)
        
        # Step 4: Analyze with LLM
        analysis = self.analyze_with_llm(logs, workflow_info, repo_structure)
        
        print(f"\n📋 Analysis Summary:")
        print(f"{'='*60}")
        print(analysis['analysis'])
        print(f"{'='*60}\n")
        
        # Step 5: Create a new branch
        fix_branch = f"gha-autofix-{run_id}"
        self.create_branch(owner, repo, base_branch, fix_branch)
        
        # Step 6: Apply fixes
        if analysis.get('files_to_change'):
            self.apply_fixes(owner, repo, fix_branch, analysis['files_to_change'])
        else:
            print("⚠️  No file changes suggested by the LLM")
            return None
        
        # Step 7: Create pull request
        pr_url = self.create_pull_request(
            owner=owner,
            repo=repo,
            head_branch=fix_branch,
            base_branch=base_branch,
            title=analysis.get('pr_title', 'Fix GitHub Actions workflow failure'),
            description=analysis.get('pr_description', 'Automated fix for workflow failure')
        )
        
        print(f"\n🎉 Auto-fix complete! Pull request: {pr_url}")
        return pr_url


def main():
    """Main entry point"""
    print("=" * 70)
    print("  GitHub Actions Auto-Fixer with Google Gemini AI")
    print("=" * 70)
    print()
    
    # Get credentials from environment variables
    github_token = os.getenv("GITHUB_TOKEN")
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    if not github_token:
        print("❌ Error: GITHUB_TOKEN environment variable not set")
        print("   Create a token at: https://github.com/settings/tokens")
        sys.exit(1)
    
    if not gemini_api_key:
        print("❌ Error: GEMINI_API_KEY environment variable not set")
        print("   Get your API key at: https://aistudio.google.com/app/apikey")
        sys.exit(1)
    
    print(f"ℹ️  Using model: {model}")
    print()
    
    # Get run URL from command line or use default
    if len(sys.argv) > 1:
        run_url = sys.argv[1]
    else:
        run_url = "https://github.com/niravpanchal11/full-stack-devops/actions/runs/22014867007"
        print(f"ℹ️  No URL provided, using default: {run_url}\n")
    
    # Initialize and run the fixer
    try:
        fixer = GHAAutoFixer(github_token, gemini_api_key, model)
        pr_url = fixer.fix_workflow(run_url)
        
        if pr_url:
            print("\n" + "=" * 70)
            print("✅ SUCCESS! Review the PR and merge when ready.")
            print("=" * 70)
        else:
            print("\n" + "=" * 70)
            print("⚠️  Process completed but no PR was created.")
            print("=" * 70)
            
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()