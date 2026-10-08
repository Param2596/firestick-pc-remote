"""Fullscreen TV home. The window title stays "Apps" so the remote can find it."""

import json
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk

YOUTUBE = Path.home() / "AppData/Local/yt-tv/open.ps1"
SPOTIFY = Path.home() / "AppData/Local/spotify-tv/open.ps1"
HOTSTAR = Path.home() / "AppData/Local/hotstar-tv/open.ps1"
BG = Path.home() / "AppData/Local/tv-apps/bg.jpg"
LOGOS = Path.home() / "AppData/Local/tv-apps/logos"
WEATHER_FILE = Path.home() / "AppData/Local/tv-apps/weather.json"
CACHE_SECONDS = 30 * 60

APPS = [
    {"name": "YouTube", "logo": "youtube", "script": YOUTUBE},
    {"name": "Spotify", "logo": "spotify", "script": SPOTIFY},
    {"name": "JioHotstar", "logo": "hotstar", "script": HOTSTAR},
]

TILE = (280, 158)


PLACE = "Mandi Gobindgarh"
# Town centre. The network lookup was resolving this connection to Amritsar.
PLACE_LAT = 30.6648
PLACE_LON = 76.2998


def greeting(now):
    hour = now.hour
    if hour < 5 or hour >= 21:
        hello = "Good Night"
    elif hour < 12:
        hello = "Good Morning"
    elif hour < 17:
        hello = "Good Afternoon"
    else:
        hello = "Good Evening"
    return f"{hello}, Param"


def condition_name(code):
    if code == 0:
        return "Clear"
    if code in (1, 2, 3):
        return "Cloudy"
    if code in (45, 48):
        return "Fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "Rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "Snow"
    if code >= 95:
        return "Storm"
    return "Cloudy"


def read_cached_weather():
    try:
        data = json.loads(WEATHER_FILE.read_text(encoding="utf-8"))
        if data.get("city") == PLACE and time.time() - data["at"] < CACHE_SECONDS:
            return data
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return None


def fetch_weather():
    cached = None
    try:
        cached = json.loads(WEATHER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    fresh = read_cached_weather()
    if fresh:
        return fresh
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={PLACE_LAT}&longitude={PLACE_LON}"
            "&current=temperature_2m,weather_code"
        )
        with urllib.request.urlopen(url, timeout=3) as response:
            current = json.load(response)["current"]
        data = {
            "city": PLACE,
            "temp": round(current["temperature_2m"]),
            "condition": condition_name(current["weather_code"]),
            "at": time.time(),
        }
        WEATHER_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEATHER_FILE.write_text(json.dumps(data), encoding="utf-8")
        return data
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
        return cached


def weather_plate():
    card = Image.new("RGBA", (380, 92), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, 379, 91), radius=18, fill=(0, 0, 0, 110))
    return card


def cover(path, size):
    photo = Image.open(path).convert("RGB")
    scale = max(size[0] / photo.width, size[1] / photo.height)
    photo = photo.resize(
        (round(photo.width * scale), round(photo.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = (photo.width - size[0]) // 2
    top = (photo.height - size[1]) // 2
    return photo.crop((left, top, left + size[0], top + size[1])).convert("RGBA")


def edge_fade(size):
    fade = Image.new("L", (1, 200))
    for band in range(200):
        fade.putpixel((0, band), int(150 * (1 - band / 200)))
    fade = fade.resize((size[0], 200))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    black = Image.new("RGBA", (size[0], 200), (0, 0, 0, 255))
    layer.paste(black, (0, 0), fade)
    layer.paste(black, (0, size[1] - 200), fade.transpose(Image.Transpose.FLIP_TOP_BOTTOM))
    return layer


def without_frame(path):
    image = Image.open(path).convert("RGB")
    width, height = image.size
    mid = height // 2
    band = width // 10

    def lum(x):
        red, green, blue = image.getpixel((x, mid))
        return red + green + blue

    peak = max(range(8, band), key=lum)
    level = lum(peak)
    inset = peak
    for x in range(peak, band):
        if lum(x) < level * 0.35:
            inset = x + width // 45
            break
    image = image.crop((inset, inset, width - inset, height - inset))
    image = image.resize(TILE, Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", TILE, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, TILE[0] - 1, TILE[1] - 1), radius=18, fill=255)
    image.putalpha(mask)
    red, green, blue, alpha = image.split()
    dim = Image.merge(
        "RGBA",
        (
            red.point(lambda v: int(v * 0.55)),
            green.point(lambda v: int(v * 0.55)),
            blue.point(lambda v: int(v * 0.55)),
            alpha,
        ),
    )
    return image, dim


class Home:
    def __init__(self, root):
        self.root = root
        self.index = 0
        self.ready = False
        self.hold = []
        self.tiles = []
        self.tile_ids = []
        self.report = None
        self.fetching = False
        self.weather_photo = None
        self.weather_card_id = None
        root.title("Apps")
        root.configure(bg="black")
        root.attributes("-fullscreen", True)
        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        for key in ("<Left>", "<Right>", "<Up>", "<Down>", "<Return>", "<Escape>"):
            root.bind(key, self.on_key)
        root.bind("<Configure>", self.on_resize)
        root.after(1000, self.tick)
        root.focus_force()

    def on_resize(self, _event):
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        if width < 100 or height < 100:
            return
        if self.ready and (width, height) == self.size:
            return
        self.size = (width, height)
        self.build()

    def build(self):
        width, height = self.size
        scene = cover(BG, self.size)
        scene.alpha_composite(Image.new("RGBA", self.size, (0, 0, 0, 70)))
        scene.alpha_composite(edge_fade(self.size))
        self.hold = [ImageTk.PhotoImage(scene)]
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.hold[0], anchor="nw")
        self.greet_id = self.canvas.create_text(
            72, 64, text="", anchor="w", fill="white", font=("Segoe UI", 36, "bold")
        )
        self.weather_card_id = None
        self.clock_id = self.canvas.create_text(
            width - 72, 62, text="", anchor="e", fill="white", font=("Segoe UI", 32, "bold")
        )
        self.date_id = self.canvas.create_text(
            width - 72, 102, text="", anchor="e", fill="#e6e6e6", font=("Segoe UI", 14)
        )
        self.canvas.create_text(
            72, height - 42, text="Navigate     OK Open     Back", anchor="w", fill="#d2d2d2", font=("Segoe UI", 13)
        )

        gap = 36
        total = len(APPS) * TILE[0] + (len(APPS) - 1) * gap
        x = (width - total) // 2
        y = height - TILE[1] - 110
        self.tiles = []
        self.tile_ids = []
        self.tile_pos = []
        for app in APPS:
            normal, dim = without_frame(LOGOS / f"{app['logo']}.png")
            photos = (ImageTk.PhotoImage(normal), ImageTk.PhotoImage(dim))
            self.tiles.append(photos)
            self.tile_pos.append((x, y))
            self.tile_ids.append(self.canvas.create_image(x, y, image=photos[0], anchor="nw"))
            self.canvas.create_text(
                x + TILE[0] // 2,
                y + TILE[1] + 28,
                text=app["name"],
                fill="white",
                font=("Segoe UI", 16, "bold"),
            )
            x += TILE[0] + gap
        self.ready = True
        self.show_focus()
        self.paint_clock()
        if self.report:
            self.show_weather(self.report)
        self.refresh_weather()

    def paint_clock(self):
        now = datetime.now()
        self.canvas.itemconfigure(self.greet_id, text=greeting(now))
        self.canvas.itemconfigure(self.clock_id, text=now.strftime("%H:%M"))
        self.canvas.itemconfigure(self.date_id, text=now.strftime("%A, %d %B"))

    def tick(self):
        if self.ready:
            self.paint_clock()
            self.refresh_weather()
        self.root.after(30000, self.tick)

    def refresh_weather(self):
        if self.fetching:
            return
        if self.report and time.time() - self.report["at"] < CACHE_SECONDS:
            return
        self.fetching = True
        threading.Thread(target=self.load_weather, daemon=True).start()

    def load_weather(self):
        data = fetch_weather()
        self.fetching = False
        if data:
            self.root.after(0, lambda: self.show_weather(data))

    def show_weather(self, data):
        if not self.ready:
            self.report = data
            return
        self.report = data
        if self.weather_card_id is None:
            self.weather_photo = ImageTk.PhotoImage(weather_plate())
            self.weather_card_id = self.canvas.create_image(64, 108, image=self.weather_photo, anchor="nw")
            self.city_id = self.canvas.create_text(
                84, 136, text="", anchor="w", fill="#e6e6e6", font=("Segoe UI", 14)
            )
            self.detail_id = self.canvas.create_text(
                84, 170, text="", anchor="w", fill="white", font=("Segoe UI", 22, "bold")
            )
        self.canvas.itemconfigure(self.city_id, text=data["city"])
        self.canvas.itemconfigure(self.detail_id, text=f"{data['temp']}°  {data['condition']}")

    def show_focus(self):
        for number, item in enumerate(self.tile_ids):
            chosen = number == self.index
            self.canvas.itemconfigure(item, image=self.tiles[number][0 if chosen else 1])

    def on_key(self, event):
        if event.keysym == "Escape":
            self.root.destroy()
        elif event.keysym in ("Left", "Up"):
            self.index = (self.index - 1) % len(APPS)
            self.show_focus()
        elif event.keysym in ("Right", "Down"):
            self.index = (self.index + 1) % len(APPS)
            self.show_focus()
        elif event.keysym == "Return":
            self.open_selected()

    def open_selected(self):
        script = APPS[self.index]["script"]
        if script.exists():
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self.root.destroy()


def main():
    root = tk.Tk()
    Home(root)
    root.mainloop()


if __name__ == "__main__":
    main()
