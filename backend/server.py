import time
import os
from datetime import datetime
import pytz
from flask import Flask, send_from_directory, jsonify, request


app = Flask(__name__, static_folder='static/angular')
last_state = {'status': 'unknown', 'timestamp': 0}
last_supervisor_state = {'status': 'unknown', 'timestamp': 0}

@app.route('/api/update-supervisor-state', methods=['POST'])
def update_supervisor_state():
    supervisor_state = request.get_json()
    if not supervisor_state:
        return jsonify({'error': 'Missing supervisor state'}), 400
    if 'status' not in supervisor_state:
        return jsonify({'error': 'Missing status'}), 400

    global last_supervisor_state
    last_supervisor_state = supervisor_state
    last_supervisor_state['time'] = time.time()
    return '', 204

@app.route('/api/update-state', methods=['POST'])
def update_state():
    state = request.get_json()
    if not state or 'status' not in state:
        return jsonify({'error': 'Missing state or status'}), 400
    
    # Save/update status
    global last_state
    last_state = state
    last_state['time'] = time.time()
    return '', 204

@app.route('/api/state')
def get_state():
    return jsonify(last_state)

@app.route('/api/supervisor-state')
def get_supervisor_state():
    return jsonify(last_supervisor_state)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    print(f"Requested path: {path}")
    
    # Adjust to include 'browser' as a subfolder
    full_path = os.path.join(app.static_folder, 'browser', path)
    print(f"Full path: {full_path}")
    
    # Check if the requested file exists
    if path != "" and os.path.exists(full_path):
        print("Serving static file:", path)
        return send_from_directory(os.path.join(app.static_folder, 'browser'), path)
    else:
        print("Serving index.html")
        return send_from_directory(os.path.join(app.static_folder, 'browser'), 'index.html')