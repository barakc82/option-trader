import time
import os
from flask import Flask, send_from_directory, jsonify, request


app = Flask(__name__, static_folder='static/angular')
last_state = {'status': 'unknown', 'timestamp': 0}

@app.route('/api/update-state', methods=['POST'])
def update_state():
    state = request.get_json()
    if not state or 'status' not in state:
        return jsonify({'error': 'Missing user_id or status'}), 400
    
    status = state['status']
    
    # Save/update status
    last_state = status
    last_state['timestamp'] = time.time()
    last_state['time'] = datetime.fromtimestamp(last_state['timestamp']).strftime("%d/%m/%Y %H:%M")

@app.route('/api/state')
def get_state():
    return jsonify(last_state)

@app.route('/api/hello')
def hello():
    return jsonify(message="Hello from Flask!")

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