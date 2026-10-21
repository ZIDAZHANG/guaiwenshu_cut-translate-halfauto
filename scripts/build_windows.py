"""Build a whole-folder Windows distribution without copying private settings."""
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
result = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                         "--distpath", str(root / "dist"), "--workpath", str(root / "build/work"),
                         str(root / "build/windows.spec")], cwd=root)
if result.returncode:
    raise SystemExit(result.returncode)
for folder in ("tools", "config"):
    target = root / "dist" / folder
    target.mkdir(parents=True, exist_ok=True)
    for source in (root / "app" / folder).iterdir():
        if source.is_file() and (source.suffix == ".py" if folder == "tools" else source.name in {
            "roast_pipeline.example.json", "translation_style_prompt.txt", "translation_glossary.json",
            "dict_pre.txt", "dict_gpt.txt", "dict_after.txt"}):
            shutil.copy2(source, target / source.name)
print("Distribution:", root / "dist", "(keep the complete folder)")
