# GitHub Setup Instructions

Your project is ready to push to GitHub! Here's how to complete the setup:

## Step 1: Create GitHub Repository

1. Go to https://github.com
2. Click the "+" icon in the top right
3. Select "New repository"
4. Fill in the details:
   - **Repository name**: `user-study-calendar`
   - **Description**: `Interactive calendar booking system for user studies with admin approval workflow`
   - **Visibility**: Public (recommended) or Private
   - **DO NOT** initialize with README (we already have one)
5. Click "Create repository"

## Step 2: Connect and Push

After creating the repository, GitHub will show you commands. Run these in your project folder:

```bash
# Add the GitHub remote (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/user-study-calendar.git

# Push to GitHub
git branch -M main
git push -u origin main
```

## Step 3: Deploy to Render

Once pushed to GitHub:

1. Go to https://render.com
2. Sign up with your GitHub account
3. Click "New +" → "Web Service"
4. Connect your `user-study-calendar` repository
5. Use these settings:
   - **Name**: user-study-calendar
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn Main:app`
6. Add environment variables (copy from your .env file)
7. Deploy!

## Your Repository Includes:

✅ Complete Flask application (`Main.py`)
✅ Dependencies (`requirements.txt`)
✅ Deployment config (`Procfile`, `render.yaml`)
✅ Documentation (`README.md`)
✅ Example configuration files
✅ Proper .gitignore (excludes sensitive files)

## Next Steps After Deployment:

1. Update Google OAuth settings with your new public URL
2. Test the calendar with real participants
3. Monitor the admin dashboard for booking requests

Your calendar will be publicly accessible worldwide! 🌍📅