"""
Noisy-Neighbor Dashboard — Flask + Socket.IO Backend
=====================================================

Live daemon'dan event'leri alır ve tarayıcılara WebSocket ile iletir.
Geçmiş event'leri in-memory ring buffer'da saklar.

Endpoint'ler:
  GET  /              — Ana sayfa (4 view içeren single-page app)
  GET  /api/events    — Son N mitigation event'i
  GET  /api/stats     — Genel istatistikler
  GET  /api/policies  — Aktif mitigation policy'leri

Socket.IO event'leri:
  ← telemetry_update  (daemon → server)
  ← mitigation_starting
  ← mitigation_result
  ← mitigation_released
  → update_dashboard  (server → browser)
  → mitigation_event  (server → browser)
"""

from collections import deque
from datetime import datetime
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

# ============================================================
# Flask App
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'noisy-neighbor-dev-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# ============================================================
# In-memory State (ring buffer)
# ============================================================
EVENT_HISTORY_SIZE = 200
TELEMETRY_HISTORY_SIZE = 300  # ~5 dakika @ 1Hz

_events = deque(maxlen=EVENT_HISTORY_SIZE)
_telemetry = deque(maxlen=TELEMETRY_HISTORY_SIZE)
_stats = {
    'total_samples': 0,
    'total_alarms': 0,
    'total_mitigations': 0,
    'total_releases': 0,
    'daemon_connected': False,
}


def _record_event(event_type, payload):
    """Bir event'i geçmişe kaydet."""
    _events.append({
        'type': event_type,
        'ts': datetime.utcnow().isoformat() + 'Z',
        'payload': payload,
    })


# ============================================================
# Routes
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/events')
def api_events():
    """Son N mitigation event'ini döndür."""
    return jsonify(list(_events)[-50:])


@app.route('/api/telemetry')
def api_telemetry():
    """Son telemetry sample'larını döndür."""
    return jsonify(list(_telemetry)[-100:])


@app.route('/api/stats')
def api_stats():
    return jsonify(_stats)


# ============================================================
# Socket.IO Handlers
# ============================================================
@socketio.on('connect')
def on_connect():
    print(f"[*] WebSocket client bağlandı")


@socketio.on('disconnect')
def on_disconnect():
    print(f"[*] WebSocket client ayrıldı")


@socketio.on('telemetry_update')
def on_telemetry(data):
    """Daemon'dan gelen telemetry verisi."""
    _stats['total_samples'] += 1
    _stats['daemon_connected'] = True
    if data.get('is_attack') == 1:
        _stats['total_alarms'] += 1

    _telemetry.append({
        'ts': datetime.utcnow().isoformat() + 'Z',
        'ipc': data.get('ipc', 0),
        'mbps': data.get('mbps', 0),
        'llc_misses': data.get('llc_misses', 0),
        'confidence': data.get('confidence', 0),
        'is_attack': data.get('is_attack', 0),
        'mitigation_active': data.get('mitigation_active', False),
    })

    # Tarayıcılara yayınla
    socketio.emit('update_dashboard', data)


@socketio.on('mitigation_starting')
def on_mitigation_starting(data):
    _stats['total_mitigations'] += 1
    _record_event('mitigation_starting', data)
    socketio.emit('mitigation_event', {
        'phase': 'starting',
        'aggressor_core': data.get('aggressor_core'),
    })
    print(f"[!] MITIGATION TETİKLENDİ — aggressor: core {data.get('aggressor_core')}")


@socketio.on('mitigation_result')
def on_mitigation_result(data):
    _record_event('mitigation_result', data)
    socketio.emit('mitigation_event', {
        'phase': 'result',
        'recovery_percent': data.get('recovery_percent', 0),
        'status': data.get('status', 'unknown'),
        'before_ipc': data.get('before_ipc', 0),
        'after_ipc': data.get('after_ipc', 0),
    })
    print(f"[+] MITIGATION SONUCU: {data.get('status')} "
          f"(recovery: {data.get('recovery_percent', 0):+.1f}%)")


@socketio.on('mitigation_released')
def on_mitigation_released(data):
    _stats['total_releases'] += 1
    _record_event('mitigation_released', data)
    socketio.emit('mitigation_event', {
        'phase': 'released',
    })
    print("[*] Mitigation kaldırıldı (sistem temiz)")


# ============================================================
# Main
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("  Noisy-Neighbor Dashboard Server")
    print("  http://localhost:5000")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
