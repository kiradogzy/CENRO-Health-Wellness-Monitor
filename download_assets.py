import os
import urllib.request

base_dir = r"c:\Users\YANI\Desktop\cenro_dc_health_and_wellness_monitoring_system\static"

files = [
    ("css/choices.min.css", "https://cdn.jsdelivr.net/npm/choices.js/public/assets/styles/choices.min.css"),
    ("js/choices.min.js", "https://cdn.jsdelivr.net/npm/choices.js/public/assets/scripts/choices.min.js"),
    ("css/all.min.css", "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"),
    ("webfonts/fa-solid-900.woff2", "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-solid-900.woff2"),
    ("webfonts/fa-solid-900.ttf", "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-solid-900.ttf"),
    ("webfonts/fa-regular-400.woff2", "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-regular-400.woff2"),
    ("webfonts/fa-regular-400.ttf", "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-regular-400.ttf"),
]

for rel_path, url in files:
    full_path = os.path.join(base_dir, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    print(f"Downloading {url} to {full_path}")
    urllib.request.urlretrieve(url, full_path)
print("Done!")
