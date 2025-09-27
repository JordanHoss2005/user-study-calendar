#!/usr/bin/env python3
"""
Deployment script for user study calendar
This script creates a public deployment-ready version
"""

import os
import json
import zipfile
import shutil

def create_deployment_package():
    print("Creating deployment package...")

    # Files to include in deployment
    files_to_copy = [
        'Main.py',
        'requirements.txt',
        'Procfile',
        'render.yaml'
    ]

    # Create deployment directory
    deploy_dir = 'deployment'
    if os.path.exists(deploy_dir):
        shutil.rmtree(deploy_dir)
    os.makedirs(deploy_dir)

    # Copy files
    for file in files_to_copy:
        if os.path.exists(file):
            shutil.copy2(file, deploy_dir)
            print(f"Copied {file}")

    # Create .env file for deployment with public URL
    env_content = """# Public deployment configuration
CALENDAR_ID=cb65f60de536e08aa27bc8b12406ce8df101c5f51a4dcc87ddb67fcf3864afa1@group.calendar.google.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=synlabmrgem@gmail.com
SMTP_PASS=xhst qbhm nwho myok
SMTP_FROM=User Study <synlabmrgem@gmail.com>
FLASK_SECRET=user_study_secret_key_production
FLASK_DEBUG=False
HOST_BASE=https://user-study-calendar.onrender.com
"""

    with open(f'{deploy_dir}/.env', 'w') as f:
        f.write(env_content)

    print(f"Created deployment package in '{deploy_dir}' directory")
    print("Next steps:")
    print("1. Upload the deployment folder to a free hosting service")
    print("2. Services like Render.com, Railway.app, or Heroku work well")
    print("3. The app will be publicly accessible once deployed")

if __name__ == "__main__":
    create_deployment_package()