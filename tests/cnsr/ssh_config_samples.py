"""Sample data shared by the ssh config tests."""

from cnsr.ssh.config import HostBlock

BLOCK = HostBlock("jp-1", "edge-cache-v123.example.com", "admin", 22, "~/.ssh/pub/jp-1.pub", "/a b/agent.sock")
BLOCK_TEXT = (
    "Host jp-1\n"
    "    HostName edge-cache-v123.example.com\n"
    "    User admin\n"
    "    Port 22\n"
    "    IdentityFile ~/.ssh/pub/jp-1.pub\n"
    "    IdentitiesOnly yes\n"
    '    IdentityAgent "/a b/agent.sock"\n'
)
MIXED = "# mine\nHost prod\n    HostName 192.0.2.5\n\nHost jp-1\n    HostName 192.0.2.9\n"
