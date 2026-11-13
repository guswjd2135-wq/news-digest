#!/bin/sh
# digest/ 폴더를 gh-pages 브랜치로 올려 GitHub Pages에 배포한다.
set -e
cd "$(dirname "$0")"
python3 digest.py --build-only
python3 finance_digest.py --build-only
git add -A
git commit -m "Update digest $(date +%F)" || true
git push origin main
git subtree push --prefix digest origin gh-pages
