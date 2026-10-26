import socket
import json

# Pfad zum bosminer Socket
SOCKET_PATH = "/tmp/bosminer.sock"

# Was wir senden wollen
request = {
    "command": "tuner.set_point",
    "chip_freq": 411,
    "volt_mv": 745
}

# Mit dem Unix-Socket verbinden
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect(SOCKET_PATH)
    client.sendall(json.dumps(request).encode("utf-8"))
    response = client.recv(4096)
    print("Antwort vom Miner:", response.decode("utf-8"))

