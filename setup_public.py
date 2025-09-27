#!/usr/bin/env python3
"""
Simple public deployment setup
"""

import json
import os

def setup_for_public_deployment():
    print("Setting up public calendar deployment...")

    # Public URL that will be used
    public_url = "https://user-study-calendar.onrender.com"

    # Update Google OAuth configuration
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

    # Update .env file for public URL
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

    print("Configuration complete!")
    print(f"Public calendar URL: {public_url}")
    print("Updated OAuth configuration")
    print("Updated environment variables")

    # Instructions for deployment
    print("\nNext steps for public deployment:")
    print("1. Go to https://render.com")
    print("2. Sign up with GitHub")
    print("3. Create new Web Service")
    print("4. Connect this repository")
    print("5. Use these settings:")
    print("   - Name: user-study-calendar")
    print("   - Environment: Python 3")
    print("   - Build Command: pip install -r requirements.txt")
    print("   - Start Command: gunicorn Main:app")
    print("6. Add environment variables from .env file")
    print("7. Deploy!")

if __name__ == "__main__":
    setup_for_public_deployment()