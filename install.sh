#!/bin/bash
set -e

INSTALL_DIR="/opt/switch-dashboard"

echo "=== Switch Dashboard Installation ==="

if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
fi

cp -r ./* "$INSTALL_DIR/"
cd "$INSTALL_DIR"

echo "[1/3] Installing Python dependencies..."

# Auto-detect and install pip3 if missing
if ! command -v pip3 &> /dev/null; then
    echo "pip3 not found, installing..."
    if command -v apt-get &> /dev/null; then
        apt-get update && apt-get install -y python3-pip
    elif command -v dnf &> /dev/null; then
        dnf install -y python3-pip
    elif command -v pacman &> /dev/null; then
        pacman -S --noconfirm python-pip
    fi
fi

# Verify and install python3-venv to avoid "externally-managed-environment" issues on Debian 12+ / Ubuntu 23+
USE_VENV=true
if ! python3 -c "import venv" &> /dev/null; then
    echo "python3-venv module not found."
    if command -v apt-get &> /dev/null; then
        echo "Attempting to install python3-venv..."
        apt-get update && apt-get install -y python3-venv || USE_VENV=false
    else
        USE_VENV=false
    fi
fi

if [ "$USE_VENV" = true ]; then
    echo "Creating virtual environment in $INSTALL_DIR/venv..."
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install -r requirements.txt
    PYTHON_EXEC="$INSTALL_DIR/venv/bin/python"
else
    echo "Installing packages globally (with fallback for externally managed environments)..."
    if pip3 install -r requirements.txt; then
        echo "Installation completed successfully."
    else
        echo "Attempting with --break-system-packages..."
        pip3 install -r requirements.txt --break-system-packages
    fi
    PYTHON_EXEC="/usr/bin/python3"
fi

echo "[2/3] Creating systemd service..."
cat > /etc/systemd/system/switch-dashboard.service << EOF
[Unit]
Description=Switch Dashboard - Ports and Traffic
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/switch-dashboard
ExecStart=$PYTHON_EXEC /opt/switch-dashboard/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable switch-dashboard
systemctl start switch-dashboard

echo "[3/3] Service status:"
systemctl status switch-dashboard --no-pager

echo ""
echo "=== Installation completed ==="
echo "Dashboard available at: http://192.168.1.32:8080"
echo ""
echo "If you want to use nginx as a reverse proxy (e.g. /switch):"
echo "  location /switch {"
echo "    proxy_pass http://127.0.0.1:8080;"
echo '    proxy_set_header Host $host;'
echo "  }"
