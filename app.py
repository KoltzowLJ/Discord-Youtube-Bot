import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio
import os
from dotenv import load_dotenv
import functools
from collections import deque

load_dotenv()
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

os.makedirs('downloads', exist_ok=True)

# Clean up leftover files in downloads directory at startup
for filename in os.listdir('downloads'):
    file_path = os.path.join('downloads', filename)
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
    except Exception as e:
        print(f"Failed to delete {file_path}. Reason: {e}")

ydl_opts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'extract_flat': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'no_warnings': True,
    'default_search': 'auto',
    'playlist_items': '1-25',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

class MusicPlayer:
    def __init__(self):
        self.download_queue = asyncio.Queue()
        self.play_queue = deque()
        self.play_queue_lock = asyncio.Lock()  # Lock for concurrency control
        self.current_voice_client = None
        self.voice_channel = None
        self.text_channel = None
        self.is_playing = False
        self.is_paused = False
        self.downloader_task = None
        self.player_task = None
        self.stop_event = asyncio.Event()
        self.first_song_ready = asyncio.Event()
        self.current_song_index = 0
        self.total_songs = 0
        self.inactivity_task = None
        self.current_song = None
        self.current_thumbnail = None
        self.playback_finished_event = asyncio.Event()
        self.current_audio_source = None  # Keep track of the current audio source
        self.song_count = 0  # Initialize song count to 0
        self.upcoming_queue = deque()  # New attribute for upcoming songs
        self.cancel_download_event = asyncio.Event()
        self.tasks = []  # List to keep track of async tasks

    async def connect_to_voice_channel(self, interaction):
        if interaction.user.voice is None:
            await interaction.response.send_message(
                "You need to be in a voice channel to use this command.", ephemeral=True)
            return None
        self.voice_channel = interaction.user.voice.channel
        if self.current_voice_client is None:
            try:
                self.current_voice_client = await self.voice_channel.connect()
            except Exception as e:
                print(f"Error connecting to voice channel: {e}")
                await interaction.response.send_message(
                    "Failed to connect to the voice channel.", ephemeral=True)
                return None
        elif self.current_voice_client.channel != self.voice_channel:
            # Disconnect from the current voice channel and move to the new one
            try:
                await self.current_voice_client.disconnect()
                self.current_voice_client = await self.voice_channel.connect()
            except Exception as e:
                print(f"Error moving to voice channel: {e}")
                await interaction.response.send_message(
                    "Failed to move to your voice channel.", ephemeral=True)
                return None
        return self.current_voice_client

    async def start(self, interaction):
        self.stop_event.clear()  # Reset stop_event
        self.playback_finished_event.clear()
        # Store the text channel for sending messages
        self.text_channel = interaction.channel
        if self.downloader_task is None or self.downloader_task.done():
            self.downloader_task = asyncio.create_task(self.downloader())
        if self.player_task is None or self.player_task.done():
            self.player_task = asyncio.create_task(self.player())

    async def downloader(self):
        while not self.stop_event.is_set():
            try:
                url = await self.download_queue.get()
                if url is None or self.stop_event.is_set():
                    break
                filename, title, thumbnail_url = await self.download_and_convert(url)
                if self.stop_event.is_set():
                    # Cleanup file if needed
                    await self.cleanup_file(filename)
                    break
                else:
                    async with self.play_queue_lock:
                        self.play_queue.append((filename, title, thumbnail_url))
                    if not self.first_song_ready.is_set():
                        self.first_song_ready.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error downloading {url}: {e}")
                if self.text_channel:
                    await self.text_channel.send(f"Error downloading {url}: {e}")
                continue
        print("Downloader has exited")

    async def download_and_convert(self, url):
        loop = asyncio.get_event_loop()
        ydl_opts_copy = ydl_opts.copy()
        ydl_opts_copy.pop('extract_flat', None)  # Remove 'extract_flat' to get full info

        # Add progress_hooks to ydl_opts_copy
        def progress_hook(d):
            if d['status'] == 'downloading':
                if self.cancel_download_event.is_set():
                    raise yt_dlp.utils.DownloadError("Download cancelled")

        ydl_opts_copy['progress_hooks'] = [progress_hook]  # Add the progress_hook

        with yt_dlp.YoutubeDL(ydl_opts_copy) as ydl:
            try:
                # Create a partial function to include download=True
                extract_info_func = functools.partial(ydl.extract_info, url, download=True)
                info = await loop.run_in_executor(None, extract_info_func)
                # Retrieve the processed file's path
                if 'requested_downloads' in info and len(info['requested_downloads']) > 0:
                    filename = info['requested_downloads'][0]['filepath']
                else:
                    filename = ydl.prepare_filename(info)
                title = info.get('title', 'Unknown Title')
                thumbnail_url = info.get('thumbnail')
            except Exception as e:
                print(f"Error extracting info for {url}: {e}")
                # Clean up any partial files
                temp_filename = ydl.prepare_filename(info) if 'info' in locals() else None
                if temp_filename and os.path.exists(temp_filename):
                    os.remove(temp_filename)
                raise e
        return filename, title, thumbnail_url

    async def player(self):
        await self.first_song_ready.wait()
        while not self.stop_event.is_set():
            try:
                # Get the next song from the queue
                async with self.play_queue_lock:
                    if not self.play_queue:
                        if self.downloader_task and not self.downloader_task.done():
                            # Wait for more songs to be downloaded
                            await asyncio.sleep(1)
                            continue
                        else:
                            # No more songs, exit the loop
                            if self.text_channel and self.is_playing:
                                await self.text_channel.send("No more songs in the queue.")
                            await self.start_inactivity_timer()
                            break
                    else:
                        item = self.play_queue.popleft()
                if item is None or self.stop_event.is_set():
                    continue  # Skip if the item is None or if stop event is set
                filename, title, thumbnail_url = item

                # Increment the song count before playing
                self.song_count += 1
                self.is_playing = True
                self.is_paused = False  # Reset pause state
                self.current_song = title
                self.current_thumbnail = thumbnail_url
                self.playback_finished_event.clear()

                # Calculate total songs remaining (including current)
                async with self.play_queue_lock:
                    self.total_songs = len(self.play_queue) + 1  # +1 for current song

                def after_playing(error):
                    if error:
                        print(f"Error playing audio: {error}")
                    # Close the audio source to release the file
                    if self.current_audio_source:
                        self.current_audio_source.cleanup()
                        self.current_audio_source = None
                    # Set is_playing to False
                    self.is_playing = False
                    # Signal that playback has ended
                    bot.loop.call_soon_threadsafe(self.playback_finished_event.set)

                self.current_audio_source = discord.FFmpegPCMAudio(filename)
                self.current_voice_client.play(self.current_audio_source, after=after_playing)

                # Send the now playing embed with the control buttons
                embed = discord.Embed(title="🎵 Now Playing", color=discord.Color.purple())
                embed.add_field(name=f"Song #{self.song_count}", value=title, inline=False)
                if thumbnail_url:
                    embed.set_thumbnail(url=thumbnail_url)
                embed.set_footer(text="Use the buttons below to control playback")
                view = MusicControlView(self)
                await self.text_channel.send(embed=embed, view=view)

                # Wait until the song is finished or skipped
                await self.playback_finished_event.wait()
                # Playback has finished

                # Clean up audio source and file
                if self.current_audio_source:
                    self.current_audio_source.cleanup()
                    self.current_audio_source = None
                await self.cleanup_file(filename)

                # Check if there are more songs to play
                async with self.play_queue_lock:
                    if not self.play_queue and (self.downloader_task is None or self.downloader_task.done()):
                        if self.text_channel:
                            await self.text_channel.send("No more songs in the queue.")
                        await self.start_inactivity_timer()
                        break  # Exit the loop since there are no more songs

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in player: {e}")
                if self.text_channel:
                    await self.text_channel.send(f"An error occurred during playback: {e}")
                break
        print("Player has exited")

    async def playback_ended(self):
        # This method is called when playback of a song ends
        async with self.play_queue_lock:
            if not self.play_queue and (self.downloader_task is None or self.downloader_task.done()):
                if self.text_channel:
                    await self.text_channel.send("No more songs in the queue.")
                await self.start_inactivity_timer()

    async def start_inactivity_timer(self):
        if self.inactivity_task and not self.inactivity_task.done():
            return  # Timer is already running
        self.inactivity_task = asyncio.create_task(self._inactivity_timeout())

    async def _inactivity_timeout(self):
        try:
            await asyncio.sleep(300)  # Wait for 5 minutes
            if not self.is_playing and self.current_voice_client and self.current_voice_client.is_connected():
                await self.text_channel.send("No activity for 5 minutes. Disconnecting...")
                await self.cleanup()
        except asyncio.CancelledError:
            pass

    async def cleanup_file(self, filename):
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except PermissionError:
                print(f"Could not delete file {filename}. It will be deleted later.")

    async def add_to_queue(self, url, play_next=False):
        if self.stop_event.is_set():
            # The bot is stopping or has been stopped; do not add to the queue
            return

        # Cancel inactivity timer if running
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
            self.inactivity_task = None
        
        # Extract basic info without downloading
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL({'format': 'bestaudio', 'noplaylist': False}) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        
            # Check after the potentially long operation
            if self.stop_event.is_set():
                return

        if 'entries' in info:  # It's a playlist
            entries = info['entries']
        else:  # It's a single video
            entries = [info]
        
        for entry in entries:
            if self.stop_event.is_set():
                break

            title = entry.get('title', 'Unknown Title')
            if play_next:
                self.upcoming_queue.appendleft((url, title))
                await self.download_queue.put(url)
            else:
                self.upcoming_queue.append((url, title))
                await self.download_queue.put(url)

    async def skip_to_position(self, position):
        if position < 1 or position > len(self.upcoming_queue):
            return "Invalid position. Please provide a valid queue position."
        
        # Skip current song
        if self.current_voice_client and self.current_voice_client.is_playing():
            self.current_voice_client.stop()
        
        # Remove songs before the desired position
        for _ in range(position - 1):
            self.upcoming_queue.popleft()
            async with self.play_queue_lock:
                if self.play_queue:
                    self.play_queue.popleft()
        
        return f"Skipped to position {position} in the queue."

    async def get_queue(self):
        if self.current_song:
            now_playing = self.current_song
            thumbnail_url = self.current_thumbnail
        else:
            now_playing = "No song is currently playing."
            thumbnail_url = None
        
        upcoming_songs = [f"{idx + 1}. {title}" for idx, (_, title) in enumerate(self.upcoming_queue)]
        
        if not upcoming_songs:
            upcoming_songs.append("The queue is empty.")
        
        return now_playing, upcoming_songs, thumbnail_url

    async def stop(self):
        self.stop_event.set()
        self.playback_finished_event.set()  # Ensure any waiting coroutines proceed

        # Cancel any pending tasks
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()

        # Cancel inactivity timer if running
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
            self.inactivity_task = None

        # Disconnect from voice channel if connected
        if self.current_voice_client and self.current_voice_client.is_connected():
            self.current_voice_client.stop()
            await self.current_voice_client.disconnect()
            self.current_voice_client = None

        # Reset playback states
        self.is_playing = False
        self.is_paused = False
        self.first_song_ready.clear()
        self.current_song_index = 0
        self.total_songs = 0
        self.current_song = None
        self.current_thumbnail = None
        self.current_audio_source = None
        self.song_count = 0

        # Clear and reset queues
        self.download_queue = asyncio.Queue()
        async with self.play_queue_lock:
            self.play_queue = deque()
        self.upcoming_queue = deque()

        # Remove any remaining downloaded files, including partial files
        for filename in os.listdir('downloads'):
            file_path = os.path.join('downloads', filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")

    async def cleanup(self):
        # Cancel inactivity timer if running
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
            self.inactivity_task = None

        # Cancel any pending tasks
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()

        await self.stop()
        if self.text_channel:
            await self.text_channel.send("Playback stopped and queue cleared.")

    async def pause(self):
        if self.current_voice_client and self.current_voice_client.is_playing():
            self.current_voice_client.pause()
            self.is_paused = True

    async def resume(self):
        if self.current_voice_client and self.current_voice_client.is_paused():
            self.current_voice_client.resume()
            self.is_paused = False

    async def skip_current_song(self):
        if self.current_voice_client and (self.current_voice_client.is_playing() or self.is_paused):
            self.current_voice_client.stop()
            self.is_playing = False
            self.is_paused = False
            
            async with self.play_queue_lock:
                if not self.play_queue and self.download_queue.empty():
                    return "Last track skipped. There are no more songs in the queue."
            return "Track skipped."
        return "There's nothing playing to skip."

    async def clear_upcoming_queue(self):
        # Clear the download queue
        while not self.download_queue.empty():
            self.download_queue.get_nowait()
        # Clear the play queue
        async with self.play_queue_lock:
            self.play_queue.clear()
        # Reset total_songs count
        self.total_songs = 1  # Only the current song remains

class MusicControlView(discord.ui.View):
    def __init__(self, music_player: MusicPlayer):
        super().__init__(timeout=None)
        self.music_player = music_player

    @discord.ui.button(label='Play/Pause', style=discord.ButtonStyle.primary, emoji='⏯️')
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if self.music_player.is_paused:
                await self.music_player.resume()
                await interaction.response.send_message("▶️ Music resumed.", ephemeral=True)
            else:
                await self.music_player.pause()
                await interaction.response.send_message("⏸️ Music paused.", ephemeral=True)
        except Exception as e:
            print(f"Error in play_pause_button: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ An error occurred: {e}", ephemeral=True)

    @discord.ui.button(label='Skip', style=discord.ButtonStyle.primary, emoji='⏭️')
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            result = await self.music_player.skip_current_song()
            await interaction.response.send_message(f"⏭️ {result}", ephemeral=True)
        except Exception as e:
            print(f"Error in skip_button: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ An error occurred: {e}", ephemeral=True)

    @discord.ui.button(label='Leave', style=discord.ButtonStyle.danger, emoji='🚪')
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.music_player.cleanup()
            await interaction.response.send_message("🚪 Disconnected from the voice channel.", ephemeral=True)
            # Disable the buttons after leaving
            self.disable_all_items()
            await interaction.message.edit(view=self)
        except Exception as e:
            print(f"Error in leave_button: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ An error occurred: {e}", ephemeral=True)

    @discord.ui.button(label='Clear Queue', style=discord.ButtonStyle.danger, emoji='🗑️')
    async def clear_queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.music_player.clear_upcoming_queue()
            await interaction.response.send_message("🗑️ Upcoming queue cleared. Current song will continue playing.", ephemeral=True)
        except Exception as e:
            print(f"Error in clear_queue_button: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ An error occurred: {e}", ephemeral=True)

    @discord.ui.button(label='View Queue', style=discord.ButtonStyle.secondary, emoji='📜')
    async def view_queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            now_playing, upcoming_songs, thumbnail_url = await self.music_player.get_queue()
            embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
            embed.add_field(name="🎧 Now Playing", value=now_playing, inline=False)
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            if upcoming_songs:
                embed.add_field(name="📜 Up Next", value="\n".join(upcoming_songs), inline=False)
            embed.set_footer(text="Use /play to add more songs!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"Error in view_queue_button: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=discord.Embed(title="❌ Error", description=f"An error occurred: {e}", color=discord.Color.red()), ephemeral=True)

    @discord.ui.button(label='Skip To', style=discord.ButtonStyle.primary, emoji='⏩')
    async def skip_to_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SkipToModal(self.music_player))

class SkipToModal(discord.ui.Modal, title='Skip To Position'):
    position = discord.ui.TextInput(label='Queue Position', placeholder='Enter a number')

    def __init__(self, music_player: MusicPlayer):
        super().__init__()
        self.music_player = music_player

    async def on_submit(self, interaction: discord.Interaction):
        try:
            position = int(self.position.value)
            result = await self.music_player.skip_to_position(position)
            await interaction.response.send_message(result, ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)

music_player = MusicPlayer()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Error syncing commands: {e}")

@bot.tree.command(name='play', description='Plays audio from a YouTube video or playlist')
async def play(interaction: discord.Interaction, url: str):
    if music_player.stop_event.is_set():
        # Reset or restart the music player if needed
        music_player.stop_event.clear()

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        print("Interaction timed out before we could defer it.")
        return

    voice_client = await music_player.connect_to_voice_channel(interaction)
    if voice_client is None:
        await interaction.followup.send("Failed to connect to voice channel.", ephemeral=True)
        return

    loop = asyncio.get_event_loop()

    async def process_playlist():
        try:
            ydl_opts_flat = ydl_opts.copy()
            ydl_opts_flat['extract_flat'] = True
            with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                info = await loop.run_in_executor(None, ydl.extract_info, url, False)

                # Check if stop_event is set
                if music_player.stop_event.is_set():
                    return

                if '_type' in info and info['_type'] == 'playlist':
                    entries = info.get('entries', [])
                    num_entries = len(entries)
                    await interaction.followup.send(f"Adding {num_entries} tracks to the queue. This may take a moment for the queue to load.", ephemeral=True)
                    if entries:
                        # Start the player if not already started
                        await music_player.start(interaction)
                        # Add entries to the download queue asynchronously
                        for entry in entries:
                            if music_player.stop_event.is_set():
                                break
                            if entry:
                                await music_player.add_to_queue(entry['url'])
                    else:
                        await interaction.followup.send("No entries found in playlist.", ephemeral=True)
                else:
                    # Not a playlist, process as a single video

                    # Check if stop_event is set
                    if music_player.stop_event.is_set():
                        return

                    await music_player.add_to_queue(url)
                    await interaction.followup.send("Added 1 track to the queue. This may take a moment.", ephemeral=True)
                    await music_player.start(interaction)
        except asyncio.CancelledError:
            print("process_playlist task was cancelled")
            return
        except Exception as e:
            print(f"Error in process_playlist: {e}")
            await interaction.followup.send(
                f"An error occurred while processing the playlist: {str(e)}", ephemeral=True)

    def task_done_callback(task):
        try:
            music_player.tasks.remove(task)
        except ValueError:
            pass  # Task was already removed

    # Create the task and store it
    task = asyncio.create_task(process_playlist())
    task.add_done_callback(task_done_callback)
    music_player.tasks.append(task)

@bot.tree.command(name='stop', description='Stops playing audio and clears the queue')
async def stop(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        await music_player.stop()
        await interaction.followup.send("Playback stopped and queue cleared.")
    except Exception as e:
        print(f"Error in stop command: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)

@bot.tree.command(name='skip', description='Skips the current track')
async def skip(interaction: discord.Interaction):
    try:
        result = await music_player.skip_current_song()
        await interaction.response.send_message(result)
    except Exception as e:
        print(f"Error in skip command: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)

@bot.tree.command(name='leave', description='Disconnects the bot from the voice channel and clears the queue')
async def leave(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        await music_player.cleanup()
        await interaction.followup.send("Disconnected from the voice channel and cleared the queue.")
    except Exception as e:
        print(f"Error in leave command: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)

@bot.tree.command(name='queue', description='Displays the current song queue')
async def queue(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        now_playing, upcoming_songs, thumbnail_url = await music_player.get_queue()
        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
        embed.add_field(name="🎧 Now Playing", value=now_playing, inline=False)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if upcoming_songs:
            embed.add_field(name="📜 Up Next", value="\n".join(upcoming_songs), inline=False)
        embed.set_footer(text="Use /play to add more songs!")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"Error in queue command: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=discord.Embed(title="❌ Error", description=f"An error occurred: {e}", color=discord.Color.red()), ephemeral=True)

@bot.tree.command(name='play_next', description='Adds a song to play immediately after the current song')
async def play_next(interaction: discord.Interaction, url: str):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        print("Interaction timed out before we could defer it.")
        return

    voice_client = await music_player.connect_to_voice_channel(interaction)
    if voice_client is None:
        await interaction.followup.send("Failed to connect to voice channel.", ephemeral=True)
        return

    await music_player.add_to_queue(url, play_next=True)
    await interaction.followup.send(embed=discord.Embed(title="🎵 Song Added", description="Your song will play next!", color=discord.Color.green()), ephemeral=True)
    
    if not music_player.is_playing:
        await music_player.start(interaction)

@bot.tree.command(name='clear_queue', description='Clears all upcoming songs in the queue')
async def clear_queue(interaction: discord.Interaction):
    try:
        await music_player.clear_upcoming_queue()
        await interaction.response.send_message("Upcoming queue cleared. Current song will continue playing.")
    except Exception as e:
        print(f"Error in clear_queue command: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)

bot.run(DISCORD_BOT_TOKEN)