#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/aaronsb/cdreader.git"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Colors (disable if not a terminal)
if [ -t 1 ]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    CYAN=$'\033[36m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD="" DIM="" GREEN="" YELLOW="" CYAN="" RED="" RESET=""
fi

info()  { printf "%s\n" "${CYAN}::${RESET} $1"; }
ok()    { printf "%s\n" "${GREEN}OK${RESET} $1"; }
warn()  { printf "%s\n" "${YELLOW}!!${RESET} $1"; }
fail()  { printf "%s\n" "${RED}FAIL${RESET} $1" >&2; exit 1; }
step()  { printf "\n%s\n" "${BOLD}$1${RESET}"; }

# Do not run as root
if [ "$(id -u)" -eq 0 ]; then
    fail "Do not run as root. Run as your normal user — sudo is requested only for package install."
fi

printf "\n"
printf "%s\n" "${BOLD}cdripper setup${RESET}"
printf "%s\n" "${DIM}Rip audio CDs to FLAC with MusicBrainz metadata${RESET}"
printf "\n"

# Detect distro
if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="$ID"
else
    fail "/etc/os-release not found. Cannot detect distro."
fi

info "Detected ${BOLD}${PRETTY_NAME:-$DISTRO}${RESET}"

# --- System packages (needs root) ---
step "Installing system packages"
warn "sudo will be requested once, then dropped."

case "$DISTRO" in
    arch|endeavouros|manjaro)
        sudo pacman -S --needed --noconfirm cdparanoia flac libdiscid util-linux python python-pipx
        ;;
    ubuntu|debian|kubuntu|linuxmint|pop)
        sudo apt-get update -qq
        sudo apt-get install -y -qq cdparanoia flac libdiscid0 libdiscid-dev eject python3 pipx
        ;;
    fedora)
        sudo dnf install -y cdparanoia flac libdiscid eject python3 pipx
        ;;
    *)
        fail "Unsupported distro: $DISTRO. Install manually: cdparanoia, flac, libdiscid, eject, python3, pipx"
        ;;
esac

sudo -k
ok "System packages installed. ${DIM}sudo credentials dropped.${RESET}"

# --- pipx install (user-level) ---
step "Installing cdripper via pipx"

pipx install "git+${REPO}" --force 2>&1 | tail -1

ok "cdripper installed to ${DIM}~/.local/bin${RESET}"

# --- Ensure ~/.local/bin is in PATH ---
USER_BIN="$HOME/.local/bin"
if [[ ":$PATH:" != *":$USER_BIN:"* ]]; then
    step "Adding ~/.local/bin to PATH"

    SHELL_NAME="$(basename "$SHELL")"
    case "$SHELL_NAME" in
        zsh)  RC_FILE="$HOME/.zshrc" ;;
        bash) RC_FILE="$HOME/.bashrc" ;;
        fish) RC_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" ;;
        *)    RC_FILE="" ;;
    esac

    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [ "$SHELL_NAME" = "fish" ]; then
        PATH_LINE='fish_add_path $HOME/.local/bin'
    fi

    if [ -n "$RC_FILE" ]; then
        # Only add if not already present
        if ! grep -qF '.local/bin' "$RC_FILE" 2>/dev/null; then
            printf '\n# Added by cdripper setup\n%s\n' "$PATH_LINE" >> "$RC_FILE"
            ok "Added to ${DIM}$RC_FILE${RESET}"
        else
            ok "Already in ${DIM}$RC_FILE${RESET}"
        fi
        warn "Run ${BOLD}source $RC_FILE${RESET} or open a new terminal for PATH changes."
    else
        warn "Unknown shell ($SHELL_NAME). Add manually: $PATH_LINE"
    fi
fi

# --- systemd user service ---
step "Setting up systemd user service"

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_DIR/cdripper.service" << 'EOF'
[Unit]
Description=CD Ripper - auto-rip audio CDs to FLAC
Documentation=https://github.com/aaronsb/cdreader

[Service]
Type=simple
ExecStart=%h/.local/bin/cdripper
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
ok "Service installed ${DIM}(disabled by default)${RESET}"

# --- Verify installation ---
step "Verifying installation"

TESTS_PASSED=0
TESTS_FAILED=0

check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        ok "$desc"
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        printf "%s\n" "${RED}FAIL${RESET} $desc"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
}

# System binaries
check "cdparanoia found"     command -v cdparanoia
check "flac found"           command -v flac
check "eject found"          command -v eject

# cdripper binary (use full path since PATH may not be updated yet)
CDRIPPER_BIN="$HOME/.local/bin/cdripper"
check "cdripper binary"      test -x "$CDRIPPER_BIN"
check "cdripper --version"   "$CDRIPPER_BIN" --version

# Python imports inside the pipx venv
PIPX_VENVS="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "")"
if [ -z "$PIPX_VENVS" ]; then
    # parse from pipx environment output
    PIPX_VENVS="$(pipx environment 2>/dev/null | grep PIPX_LOCAL_VENVS | cut -d= -f2)"
fi
PIPX_PYTHON="${PIPX_VENVS}/cdripper/bin/python3"

check "python: discid"          "$PIPX_PYTHON" -c "import discid"
check "python: musicbrainzngs"  "$PIPX_PYTHON" -c "import musicbrainzngs"
check "python: mutagen"         "$PIPX_PYTHON" -c "import mutagen.flac"

# libdiscid shared library
check "libdiscid.so"         "$PIPX_PYTHON" -c "import ctypes; ctypes.cdll.LoadLibrary('libdiscid.so.0')"

# systemd service file
check "systemd service file" test -f "$SERVICE_DIR/cdripper.service"

printf "\n"
if [ "$TESTS_FAILED" -eq 0 ]; then
    ok "${BOLD}All $TESTS_PASSED checks passed${RESET}"
else
    warn "${TESTS_PASSED} passed, ${RED}${TESTS_FAILED} failed${RESET}"
fi

# --- Done ---
printf "\n"
printf "%s\n" "${GREEN}${BOLD}Setup complete!${RESET}"
printf "\n"
printf "%s\n" "${BOLD}Usage:${RESET}"
printf "  %s\n" "cdripper                     ${DIM}# poll and rip to ~/Music${RESET}"
printf "  %s\n" "cdripper -d /dev/sr0         ${DIM}# specific CD device${RESET}"
printf "  %s\n" "cdripper -o /path/to/music   ${DIM}# custom output directory${RESET}"
printf "  %s\n" "cdripper --once              ${DIM}# rip one disc and exit${RESET}"
printf "\n"
printf "%s\n" "${BOLD}Auto-start on login:${RESET}"
printf "  %s\n" "systemctl --user enable --now cdripper"
printf "\n"
