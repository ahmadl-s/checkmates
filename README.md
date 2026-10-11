# Checkmates

Checkmates is an AI-powered Discord project manager for university group work. It watches project channels, captures team discussions, and turns meaningful updates into structured tasks that can be tracked over time.

## Overview

The project is designed for teams that coordinate work in Discord but still want a lightweight way to:

- create and manage a shared project
- track tasks and deadlines
- keep task status synced with team updates
- use AI to extract actionable items from conversation history
- persist project context in a local SQLite database

## Features

- Discord bot integration for project and task workflows
- project creation with team member selection
- task creation, listing, and status updates
- deadline tracking for team tasks
- message collection from specified Discord channels
- AI-based task extraction using Google Gemini
- deduplication and persistence for generated tasks
- configurable environment-based settings

## Repository layout

```text
checkmates/
├── main.py                     # lightweight Discord bot prototype
├── commands/
│   └── project.py              # slash commands for project/task management
├── backend/
│   ├── .env.example            # environment configuration template
│   ├── pyproject.toml          # package metadata and dependencies
│   ├── checkmates/
│   │   ├── __main__.py         # application entry point
│   │   ├── collector.py        # Discord message collection logic
│   │   ├── config.py           # environment settings
│   │   ├── dedup.py            # deduplication helpers
│   │   ├── filtering.py        # message filtering logic
│   │   ├── models.py           # task/message data models
│   │   ├── normalizer.py       # normalization utilities
│   │   ├── context/            # project context retrieval
│   │   ├── database/           # SQLite repository and storage code
│   │   ├── extraction/         # Gemini task extraction logic
│   │   └── processors/         # persistence and extraction processing pipeline
│   └── tests/                  # backend tests
└── README.md
```

## Tech stack

- Python 3.11+
- discord.py
- Pydantic + pydantic-settings
- SQLite (aiosqlite)
- Google Gemini API for task extraction

## Getting started

### 1. Clone the repository

```bash
git clone https://github.com/ahmadl-s/checkmates.git
cd checkmates
```

### 2. Set up the backend environment

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### 3. Configure environment variables

Copy the example environment file and fill in the required values:

```bash
cp .env.example .env
```

Example `.env` values:

```env
DISCORD_TOKEN=your_discord_bot_token
PROJECT_CHANNEL_IDS=123456789012345678,987654321098765432
GEMINI_API_KEY=your_gemini_api_key
LOG_LEVEL=INFO
```

Key settings include:

- `DISCORD_TOKEN`: Discord bot token
- `PROJECT_CHANNEL_IDS`: comma-separated Discord channel IDs to monitor
- `GEMINI_API_KEY`: Google Gemini API key for AI extraction
- `DATABASE_PATH`: SQLite database path
- `TIMEZONE`: project timezone
- `CONTEXT_MAX_MESSAGES`: recent message history used for project context

### 4. Run the bot

From the backend directory:

```bash
python -m checkmates
```

## Discord commands

The project includes Discord slash commands such as:

- `/project create`
- `/project view`
- `/project task-create`
- `/project task-list`
- `/project task-update`

These commands help a team establish a project, assign members, and manage tasks from within Discord.

## How the AI workflow works

1. The bot watches configured Discord channels.
2. Messages are stored in the local database.
3. Relevant conversation context is gathered for a project.
4. Gemini analyzes recent team discussion and extracts actionable tasks.
5. Tasks are deduplicated and saved for tracking.
6. Team members can update task status and deadlines from Discord.

## Notes

- The structure includes both a lightweight prototype (`main.py` and `commands/project.py`) and a more advanced backend package under `backend/checkmates/`.
- The backend is the main production implementation.
- The app is configured for university group project workflows, but the same structure can be adapted for other team collaboration setups.

## License

This project currently does not specify a license in the repository metadata.

## Contributing

Contributions are welcome. If you are improving the Discord bot, task extraction, or project management workflows, open a pull request with a clear description of the change.

## Contact

For questions or collaboration, contact the repository owner at `ahmadl-s` on GitHub.
