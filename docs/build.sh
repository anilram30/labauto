#!/bin/sh
# Build docs/report.pdf from report.md (pandoc + XeLaTeX).
#
#   sh docs/build.sh
#
# Needs: pandoc, XeLaTeX, lmodern (pandoc's default template loads it) and the DejaVu
# fonts including DejaVu Math TeX Gyre, which lives in fonts-dejavu-extra rather than
# fonts-dejavu. On Debian/Ubuntu:
#   sudo apt-get install pandoc texlive-xetex texlive-fonts-recommended \
#                        texlive-latex-recommended lmodern fonts-dejavu fonts-dejavu-extra
set -e
cd "$(dirname "$0")"

# Some minimal TeX installations ship without lmodern; a local stub can be placed here.
if [ -d /tmp/texmf/tex/latex/lmodern ]; then
  TEXINPUTS="/tmp/texmf/tex/latex/lmodern:${TEXINPUTS}"
  export TEXINPUTS
fi

pandoc report.md --citeproc --pdf-engine=xelatex \
  -V mainfont="DejaVu Serif" -V sansfont="DejaVu Sans" -V monofont="DejaVu Sans Mono" \
  -V mathfont="DejaVu Math TeX Gyre" -o report.pdf

echo "wrote $(pwd)/report.pdf"
