"""Export the complete local product for a public GitHub repository."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT_FILES = (
    ".env.example", "pyproject.toml", "README.md", "UI_STYLE_GUIDE.md",
    "setup_tax_tool.cmd", "setup_tax_tool.ps1", "start_tax_tool.cmd",
    "make_share_package.cmd", "make_share_package.ps1", "bot.toml",
    "download_history.cmd", "start_monitor.cmd", "start_recorder.cmd",
    "build_product_package.py",
)
DEMO_ONLY = {
    "src/backpack_tax/demo.py",
    "src/backpack_tax/static/demo.js",
    "src/backpack_tax/templates/demo_readonly.html",
    "tests/test_public_demo.py",
}
GITIGNORE = """.env
.env.*
!.env.example
.venv/
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.test*/
.demo-tools/
.vercel/
data/
imports/
dist/
build/
"""


def build_product_package(root: Path) -> Path:
    root = root.resolve()
    destination = root / "dist" / "github-product-upload"
    if destination.exists():
        destination = root / "dist" / f"github-product-upload-{datetime.now():%Y%m%d-%H%M%S-%f}"
    files = [root / name for name in ROOT_FILES]
    for directory in (root / "src" / "backpack_tax", root / "src" / "backpack_bot", root / "tests"):
        files.extend(path for path in directory.rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and path.suffix.lower() in {".py", ".html", ".css", ".js", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".ico"}
                     and path.relative_to(root).as_posix() not in DEMO_ONLY)
    # Only blank API-key placeholders belong in the public example file.
    for line in (root / ".env.example").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            if line.split("=", 1)[1].strip():
                raise ValueError(".env.example must contain only empty credential placeholders")
    destination.mkdir(parents=True)
    for source in sorted(files):
        if source.is_symlink():
            raise ValueError(f"Unexpected symlink: {source.relative_to(root)}")
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (destination / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    readme_path = destination / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    demo_heading = "## 누구나 접속하는 공개 데모"
    if demo_heading in readme:
        start = readme.index(demo_heading)
        end = readme.index("\n## ", start + len(demo_heading))
        readme = readme[:start] + readme[end + 1:]
    product_note = (
        "## 전체 제품 실행\n\n"
        "이 저장소는 자료 업로드·수정·저장·보고서 추출 기능이 활성화된 전체 제품 소스입니다.\n"
        "`start_tax_tool.cmd`를 실행하면 빈 장부로 시작하며, 사용자가 가져온 자료는 자기 PC의 `data` 폴더에 저장됩니다.\n"
        "파일을 직접 가져오는 기본 기능에는 거래소 API 키가 필요하지 않습니다.\n\n"
        "현재 제품은 개인 PC에서 사용하는 구조입니다. 여러 사용자가 인터넷에서 각자의 장부를 사용하는 서비스로 운영하려면 "
        "사용자 로그인, 사용자별 데이터 접근 제어, 영구 DB·증빙 저장소를 추가해야 합니다.\n\n"
        "## GitHub 업로드\n\n"
        "새 `coinaccount` 저장소에 이 폴더 안의 파일과 `src`, `tests` 폴더를 올리세요.\n"
        "원본 거래자료, 실제 `.env`, API 키는 포함되어 있지 않습니다.\n"
        "GitHub에서 내려받은 사용자는 Python 3.11 이상을 설치한 뒤 `start_tax_tool.cmd`로 실행할 수 있습니다.\n\n"
    )
    if "## 전체 제품 실행" not in readme:
        insertion = readme.index("## 새 Windows PC에서 실행")
        readme = readme[:insertion] + product_note + readme[insertion:]
    readme_path.write_text(readme, encoding="utf-8")
    archive_path = root / "dist" / "coinaccount-full-product.zip"
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(destination).as_posix())
    return destination


if __name__ == "__main__":
    print(build_product_package(Path(__file__).resolve().parent))
