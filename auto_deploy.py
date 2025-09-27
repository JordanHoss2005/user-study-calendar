#!/usr/bin/env python3
"""
Automatic deployment to free hosting services
"""

import os
import json
import subprocess
import time

def create_github_repo():
    """Create a GitHub repository for deployment"""
    print("Creating GitHub repository...")

    # Initialize git repository
    try:
        subprocess.run(['git', 'init'], check=True, cwd='.')
        print("Git repository initialized")

        # Add files
        subprocess.run(['git', 'add', '.'], check=True, cwd='.')
        subprocess.run(['git', 'commit', '-m', 'Initial commit - User Study Calendar'], check=True, cwd='.')
        print("Files committed to git")

        return True
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {e}")
        return False

def deploy_to_render():
    """Deploy to Render.com"""
    print("Setting up Render deployment...")

    # Create render.yaml for automatic deployment
    render_config = {
        "services": [
            {
                "type": "web",
                "name": "user-study-calendar",
                "env": "python",
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "gunicorn Main:app",
                "envVars": [
                    {"key": "CALENDAR_ID", "value": "cb65f60de536e08aa27bc8b12406ce8df101c5f51a4dcc87ddb67fcf3864afa1@group.calendar.google.com"},
                    {"key": "SMTP_HOST", "value": "smtp.gmail.com"},
                    {"key": "SMTP_PORT", "value": "587"},
                    {"key": "SMTP_USER", "value": "synlabmrgem@gmail.com"},
                    {"key": "SMTP_PASS", "value": "xhst qbhm nwho myok"},
                    {"key": "SMTP_FROM", "value": "User Study <synlabmrgem@gmail.com>"},
                    {"key": "FLASK_SECRET", "value": "user_study_secret_key_production"},
                    {"key": "FLASK_DEBUG", "value": "False"},
                    {"key": "HOST_BASE", "value": "https://user-study-calendar.onrender.com"}
                ]
            }
        ]
    }

    with open('render.yaml', 'w') as f:
        import yaml
        yaml.dump(render_config, f, default_flow_style=False)

    print("Render configuration created")

def show_deployment_info():
    """Show deployment information"""
    print("\n" + "="*60)
    print("DEPLOYMENT READY!")
    print("="*60)
    print("Your calendar is configured for public access at:")
    print("https://user-study-calendar.onrender.com")
    print()
    print("To complete deployment:")
    print("1. Go to https://render.com")
    print("2. Sign up with GitHub (free)")
    print("3. Click 'New +' -> 'Web Service'")
    print("4. Connect your GitHub repository")
    print("5. Use these settings:")
    print("   - Name: user-study-calendar")
    print("   - Environment: Python 3")
    print("   - Build Command: pip install -r requirements.txt")
    print("   - Start Command: gunicorn Main:app")
    print("6. Deploy!")
    print()
    print("Once deployed, your calendar will be accessible from anywhere!")
    print("Email links will work on any network worldwide.")
    print("="*60)

if __name__ == "__main__":
    print("Starting automatic deployment setup...")

    # Create git repository
    if create_github_repo():
        print("Git repository ready")

    # Setup deployment configuration
    deploy_to_render()

    # Show final instructions
    show_deployment_info()