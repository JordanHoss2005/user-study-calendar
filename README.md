# User Study Calendar Booking System

A Flask-based web application for managing user study participant bookings with Google Calendar integration and admin approval workflow.

## Features

- 📅 **Interactive Calendar Interface** - Participants see hour-by-hour availability in a visual calendar grid
- 🔐 **Admin Approval System** - All bookings require admin confirmation before calendar events are created
- 📧 **Email Integration** - Automatic email notifications for invitations and confirmations
- 🗓️ **Google Calendar Sync** - Seamless integration with Google Calendar for event management
- 📱 **Responsive Design** - Works on desktop and mobile devices
- 🌍 **Public Access** - Deployable to cloud platforms for worldwide accessibility

## Calendar Interface

The system displays a 7-day calendar view with:
- **Time slots**: 9:00 AM to 9:00 PM (Toronto time)
- **Visual indicators**:
  - ✅ Green = Available (clickable)
  - ❌ Red = Unavailable/Booked
  - ⏰ Gray = Past times
- **One-click booking** on available slots

## Workflow

1. **Admin adds participant** → Email with calendar link sent
2. **Participant clicks link** → Sees interactive calendar
3. **Participant selects slot** → Booking request submitted
4. **Admin reviews request** → Approves or rejects
5. **If approved** → Google Calendar invite sent automatically

## Setup

### Prerequisites

- Python 3.8+
- Google Cloud Platform account with Calendar API enabled
- Gmail account for SMTP (or other SMTP service)

### Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set up Google Calendar API:
   - Create project in Google Cloud Console
   - Enable Google Calendar API
   - Create OAuth 2.0 credentials
   - Download credentials and save as `credentials.json`

4. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

5. Run the application:
   ```bash
   python Main.py
   ```

### Environment Variables

- `CALENDAR_ID`: Your Google Calendar ID
- `SMTP_HOST`: SMTP server (e.g., smtp.gmail.com)
- `SMTP_USER`: Your email address
- `SMTP_PASS`: App password for email
- `HOST_BASE`: Base URL for your deployment

## Deployment

### Deploy to Render (Free)

1. Fork this repository
2. Sign up at [render.com](https://render.com)
3. Create new Web Service
4. Connect your GitHub repository
5. Use these settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn Main:app`
6. Add environment variables from your `.env` file
7. Deploy!

### Deploy to Railway

1. Install Railway CLI: `npm install -g @railway/cli`
2. Login: `railway login`
3. Deploy: `railway up`

## Usage

### Admin Dashboard

Access `/admin` to:
- Add new participants
- Review pending bookings
- Approve/reject booking requests
- Manage email templates
- Upload consent forms

### Participant Experience

1. Receive email with calendar link
2. Click link to view availability calendar
3. Select preferred time slot
4. Receive confirmation once approved

## Security Features

- OAuth 2.0 for Google integration
- Environment variable configuration
- CSRF protection
- Input validation and sanitization
- Secure session management

## File Structure

- `Main.py` - Main Flask application
- `requirements.txt` - Python dependencies
- `Procfile` - Deployment configuration
- `credentials.example.json` - OAuth credentials template
- `.env.example` - Environment variables template

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is for research and educational purposes.

## Support

For issues or questions, please create an issue in the GitHub repository.