#!/usr/bin/env bash
# ==============================================================================
# CemSim OTS - Domain Linking & SSL Management Tool
# Links or unlinks a hosting domain with Caddy for automatic Let's Encrypt TLS.
# ==============================================================================

set -euo pipefail

CADDY_SNIPPET="/etc/caddy/conf.d/cemsim.caddyfile"
SERVER_IPV4=$(curl -4 -s --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
SERVER_IPV6=$(curl -6 -s --max-time 3 https://api6.ipify.org 2>/dev/null || true)

usage() {
    echo "Usage:"
    echo "  sudo bash scripts/link_domain.sh <domain>       # Link domain with auto SSL/TLS"
    echo "  sudo bash scripts/link_domain.sh --status       # Show current domain & SSL config"
    echo "  sudo bash scripts/link_domain.sh --unlink <domain> # Remove domain from Caddy"
    echo ""
    echo "Example:"
    echo "  sudo bash scripts/link_domain.sh ots.example.com"
    exit 1
}

if [[ $# -eq 0 ]]; then
    usage
fi

ACTION="$1"

if [[ "$ACTION" == "--status" ]]; then
    echo "================================================================="
    echo "CemSim OTS - Current Hosting & Certification Status"
    echo "================================================================="
    echo "Public Server IPv4: $SERVER_IPV4"
    [[ -n "$SERVER_IPV6" ]] && echo "Public Server IPv6: $SERVER_IPV6"
    echo "Backend Service:    $(systemctl is-active cemsim.service || echo 'inactive')"
    echo "Caddy Proxy:        $(systemctl is-active caddy.service || echo 'inactive')"
    echo ""
    echo "Active Caddy configuration ($CADDY_SNIPPET):"
    cat "$CADDY_SNIPPET"
    exit 0
fi

if [[ "$ACTION" == "--unlink" ]]; then
    if [[ $# -lt 2 ]]; then
        echo "Error: please provide the domain to unlink."
        exit 1
    fi
    DOMAIN="$2"
    echo "Unlinking domain $DOMAIN from $CADDY_SNIPPET..."

    # Remove block for DOMAIN
    python3 -c "
import re, sys
path = '$CADDY_SNIPPET'
with open(path, 'r') as f:
    text = f.read()
pattern = r'$DOMAIN\s*\{[^}]*\}\n?'
new_text = re.sub(pattern, '', text)
with open(path, 'w') as f:
    f.write(new_text)
"
    caddy validate --config /etc/caddy/Caddyfile
    systemctl reload caddy
    echo "Successfully unlinked $DOMAIN and reloaded Caddy."
    exit 0
fi

DOMAIN="$ACTION"

# Check if domain looks valid
if [[ ! "$DOMAIN" =~ ^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
    echo "Error: '$DOMAIN' does not appear to be a valid domain name."
    exit 1
fi

echo "================================================================="
echo "Linking CemSim OTS with Domain: $DOMAIN"
echo "================================================================="

# DNS check
RESOLVED_IP=$(dig +short "$DOMAIN" | tail -n1 || true)
if [[ -z "$RESOLVED_IP" ]]; then
    echo "Warning: DNS for '$DOMAIN' currently does not resolve to an IP."
    echo "Make sure to create an DNS A Record pointing '$DOMAIN' -> '$SERVER_IPV4'."
elif [[ "$RESOLVED_IP" != "$SERVER_IPV4" ]]; then
    echo "Notice: DNS for '$DOMAIN' resolves to '$RESOLVED_IP', while this server is '$SERVER_IPV4'."
    echo "If you just updated DNS, allow a few minutes for propagation."
else
    echo "DNS verified: '$DOMAIN' points to this server ($SERVER_IPV4)."
fi


# Check if domain is already in snippet
if grep -q "$DOMAIN" "$CADDY_SNIPPET"; then
    echo "Domain '$DOMAIN' is already configured in $CADDY_SNIPPET."
else
    echo "Adding domain block for '$DOMAIN'..."
    cat << EOF >> "$CADDY_SNIPPET"

$DOMAIN {
    reverse_proxy localhost:8000
}
EOF
fi

echo "Validating Caddy configuration..."
if caddy validate --config /etc/caddy/Caddyfile; then
    echo "Reloading Caddy..."
    systemctl reload caddy
    echo "================================================================="
    echo "Success! '$DOMAIN' is now linked to CemSim OTS."
    echo "Caddy will automatically provision a Let's Encrypt / ZeroSSL TLS certificate."
    echo "Visit: https://$DOMAIN"
    echo "================================================================="
else
    echo "Caddy validation failed. Reverting changes..."
    exit 1
fi
