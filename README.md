# Discord Music Bot

This Discord bot is a demonstration project that allows users to play music in voice channels. It uses yt-dlp to fetch audio from various sources.

## Disclaimer

This project is for educational and demonstration purposes only. It is not intended for production use or to circumvent any terms of service.

## Features

- Play audio from YouTube videos and playlists
- Queue management
- Basic playback controls (pause, resume, skip, etc.)
- Display current queue and now playing information

## Installation

1. Clone this repository
2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Set up your Discord bot token in a `.env` file
4. Run the bot:
   ```
   python app.py
   ```

## Docker

A Dockerfile is provided for easy deployment. To build and run the Docker container:

```bash
docker build -t discord-music-bot .
docker run -d --name music-bot discord-music-bot
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- This project uses [yt-dlp](https://github.com/yt-dlp/yt-dlp), a fork of youtube-dl
- [discord.py](https://github.com/Rapptz/discord.py) for Discord API integration