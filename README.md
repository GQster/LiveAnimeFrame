# LiveAnimeFrame
Displays shows in a round robin format.


## Features
exact timestamp resume per-episode

persistent SQLite DB (shows, per-show progress, timestamps)

nice Flask web UI with a grid of cards (posters/thumbnails, current episode name, progress)

drag/drop reorder, add/remove, restart show

optional schedule (enable/disable + start/end times) configurable in the web UI

poster detection (poster.jpg|png, cover.jpg|png), and automatic thumbnail extraction from the first video if no poster (uses ffmpeg)

BH1750 light sensor control (lux threshold) and schedule both respected (play when BOTH allow playback unless you disable the light sensor in UI)

safe Kodi JSON-RPC handling (handles Kodi offline gracefully)

systemd-friendly (saves DB on changes and at shutdown)


## Setup

### Pre Recs
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv ffmpeg i2c-tools -y
sudo raspi-config    # enable I2C
// create virtualenv and install python deps
python3 -m venv ~/anime-env
source ~/anime-env/bin/activate
pip install flask requests smbus2 python-dotenv
// create dirs
sudo mkdir -p /media/anime
mkdir -p /home/pi/anime_static
// place a fallback poster at /home/pi/anime_static/fallback.png


Make sure Kodi is configured to allow remote control via HTTP (Settings → Services → Control → Allow remote control via HTTP). Port 8080 default.



### Notes, tips & troubleshooting

1. PIL (Pillow): The tiny fallback poster generator uses Pillow. If you get an ImportError, install it:

source ~/anime-env/bin/activate
pip install Pillow


2. ffmpeg: Required for thumbnail extraction. Already installed by the apt install ffmpeg line.


3. Kodi auth: If you set a username/password in Kodi HTTP settings, populate KODI_AUTH accordingly at top of the script.


4. Permissions: Ensure the user running the script (e.g., pi) can read /media/anime and write to /home/pi/anime_static and /home/pi/anime_frame.db.


5. Autostart: Use systemd (recommended). Example service file /etc/systemd/system/animeframe.service:

[Unit]
Description=Anime Frame v2
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi
ExecStart=/home/pi/anime-env/bin/python /home/pi/anime_frame_v2.py
Restart=always

[Install]
WantedBy=multi-user.target

Then sudo systemctl daemon-reload && sudo systemctl enable --now animeframe.service.


6. Web UI: Visit http://<pi_ip>:5000/. The grid shows poster thumbnails, current episode file name (if available), and saved position.


7. Schedule behaviour: Playback requires both the schedule and the light sensor (if both enabled) to allow play. In the UI you can turn either off; if both disabled the system will only be controlled by user-initiated play/pause.


8. Rotating / round-robin: After a series finishes, it resets episode index to 0 and pushes that show to the end of the playlist order so rotation continues naturally (as you requested: finished series restart, but also keep round-robin).


9. Edge cases: If a show folder has no video files it will be rotated to the end; you can remove it from the UI.
