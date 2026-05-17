from flask import Flask, render_template
from flask_socketio import SocketIO

app = Flask(__name__)
# Enable CORS so the daemon can connect easily
socketio = SocketIO(app, cors_allowed_origins="*")

@app.route('/')
def index():
    # Serves the frontend HTML page
    return render_template('index.html')

@socketio.on('telemetry_update')
def handle_telemetry(data):
    # Receive data from live_daemon.py and broadcast it to the web browser
    socketio.emit('update_dashboard', data)

if __name__ == '__main__':
    print("[*] Starting Noisy-Neighbor Live Dashboard on port 5000...")
    # Run on 0.0.0.0 so you can access it via the machine's IP address
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
