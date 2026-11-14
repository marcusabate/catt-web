# catt-web

**Cast local videos to a Chromecast and control them from your phone. A web remote built on [catt](https://github.com/skorokithakis/catt).**

Pick a file on your computer, cast it, and control it from a browser on the computer or a phone on the same Wi-Fi.

- Play/pause, seek, ±10/30 s, volume, mute, stop and skip
- Subtitles from `.srt`/`.vtt`/`.ass` files or from embedded tracks (converted to WebVTT)
- Codec check with ffprobe, plus a one-click convert (video copied, audio to stereo AAC) into `/tmp/catt-web/`
- Cast URLs (through yt-dlp), websites (DashCast) and a YouTube queue
- Blocks system sleep while a local file plays, and offers to resume after wake or restart
- QR code in the terminal and on the page (Phone button) to open the remote on a phone

## Requirements

- Linux with systemd (for the sleep lock)
- Python 3.10+
- `ffmpeg` and `ffprobe`

## Install

Install with pipx:

```sh
pipx install catt
pipx inject catt segno
ln -s "$PWD/catt-web" ~/.local/bin/catt-web
```

Or install into a venv in the project:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Use

```sh
catt-web [--device NAME|IP] [--port 8765] [--root ~] [--local-only] [--no-browser]
```

At start, catt-web prints two URLs: one for this computer and one for a phone on the same network. Both URLs include an access token.

### Open on a phone

- **Terminal:** under the phone URL, catt-web prints a QR code. Scan it with the phone camera. The phone must be on the same Wi-Fi.
- **Page:** on the computer, click **Phone** in the header to show the same QR code. The page makes a new code each time, so it is correct if the computer's Wi-Fi IP changes.

The QR code needs `segno`. Without it, catt-web prints the URL only. The terminal QR code is printed only when the output goes to a terminal. With `--local-only`, there is no phone URL and no QR code.

### Access token

- The token is saved in `~/.config/catt-web/config.json` and stays the same between runs, so a bookmark keeps working.
- When you open the link, the page saves the token in that browser and removes it from the address bar.
- If a browser shows "No valid access token", open the link again or scan the QR code.
- To make a new token (for example, to lock out a device), stop catt-web, delete the `token` entry in the config file, and start catt-web again.

Notes:
- The computer serves the media to the TV, so playback stops if the computer goes to sleep.
- Chromecast Gen 2 and Ultra devices do not play multichannel AAC. Use Convert for these files.

## License

MIT, see [LICENSE](LICENSE). catt-web is not affiliated with the catt project.

catt-web uses these packages without including them:

| Package | License |
|---|---|
| [catt](https://github.com/skorokithakis/catt) | BSD-2-Clause |
| [PyChromecast](https://github.com/home-assistant-libs/pychromecast) | MIT |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | LGPL-2.1-or-later |
| [Requests](https://github.com/psf/requests) | Apache-2.0 |
| [segno](https://github.com/heuer/segno) | BSD-3-Clause |

catt-web also runs FFmpeg as a separate program.
