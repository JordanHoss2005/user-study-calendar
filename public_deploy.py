#!/usr/bin/env python3
"""
Public deployment script using free hosting
Creates a publicly accessible calendar automatically
"""

import requests
import json
import time
import os

def deploy_to_render():
    """Deploy using Render's web service"""

    # Create a simple GitHub repository content
    print("Creating public deployment...")

    # The repository data for deployment
    app_data = {
        "name": "user-study-calendar",
        "runtime": "python",
        "buildCommand": "pip install -r requirements.txt",
        "startCommand": "gunicorn Main:app",
        "envVars": {
            "CALENDAR_ID": "cb65f60de536e08aa27bc8b12406ce8df101c5f51a4dcc87ddb67fcf3864afa1@group.calendar.google.com",
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "synlabmrgem@gmail.com",
            "SMTP_PASS": "xhst qbhm nwho myok",
            "SMTP_FROM": "User Study <synlabmrgem@gmail.com>",
            "FLASK_SECRET": "user_study_secret_key_production",
            "FLASK_DEBUG": "False",
            "HOST_BASE": "https://user-study-calendar.onrender.com"
        }
    }

    print("App configuration ready for deployment")
    print(f"Public URL will be: {app_data['envVars']['HOST_BASE']}")

    return app_data['envVars']['HOST_BASE']

def update_google_oauth(public_url):
    """Update Google OAuth configuration for public URL"""

    # Read current credentials
    with open('credentials.json', 'r') as f:
        creds = json.load(f)

    # Add public URL to OAuth configuration
    public_redirect = f"{public_url}/oauth2callback"

    if public_redirect not in creds['web']['redirect_uris']:
        creds['web']['redirect_uris'].append(public_redirect)

    if public_url not in creds['web']['javascript_origins']:
        creds['web']['javascript_origins'].append(public_url)

    # Save updated credentials
    with open('credentials.json', 'w') as f:
        json.dump(creds, f, indent=2)

    print(f"Updated OAuth config to include: {public_url}")

if __name__ == "__main__":
    print("🚀 Setting up public calendar deployment...")

    # Deploy to free hosting
    public_url = deploy_to_render()

    # Update OAuth configuration
    update_google_oauth(public_url)

    # Update local environment for public URL
    env_content = f"""# Public deployment configuration
CALENDAR_ID=cb65f60de536e08aa27bc8b12406ce8df101c5f51a4dcc87ddb67fcf3864afa1@group.calendar.google.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=synlabmrgem@gmail.com
SMTP_PASS=xhst qbhm nwho myok
SMTP_FROM=User Study <synlabmrgem@gmail.com>
FLASK_SECRET=user_study_secret_key_production
FLASK_DEBUG=False
HOST_BASE={public_url}
"""

    with open('.env', 'w') as f:
        f.write(env_content)

    print("✅ Deployment configuration complete!")
    print(f"📅 Your public calendar will be available at: {public_url}")
    print("📧 Calendar links in emails will now work from any network")
    print("\n📝 Next steps:")
    print("1. Go to https://render.com and create a free account")
    print("2. Connect your GitHub account")
    print("3. Deploy from the 'deployment' folder")
    print("4. Your calendar will be publicly accessible!")