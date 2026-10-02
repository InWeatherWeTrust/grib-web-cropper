import sys
import os
import json
import shutil
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/clean', methods=['POST'])
def clean_server_dir():
    try:
        shutil.rmtree(UPLOAD_FOLDER)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        return jsonify({"success": True, "message": "Stockage cloud vidé avec succès ! (0 Mo occupés)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload_chunk', methods=['POST'])
def upload_chunk():
    """Réceptionne et assemble les petits morceaux de 5 Mo envoyés par le navigateur"""
    file = request.files['file']
    filename = request.form.get('filename')
    chunk_index = int(request.form.get('chunkIndex'))
    total_chunks = int(request.form.get('totalChunks'))
    
    # Fichier temporaire d'assemblage
    temp_path = os.path.join(UPLOAD_FOLDER, "raw_" + filename)
    
    # Mode 'ab' (append binary) : on écrit à la suite du fichier existant
    with open(temp_path, 'ab') as f:
        f.write(file.read())
        
    return jsonify({"success": True, "message": f"Morceau {chunk_index + 1}/{total_chunks} reçu."})

@app.route('/api/crop_chunked', methods=['POST'])
def crop_chunked():
    """Lance la découpe une fois le fichier entièrement reconstruit sur le serveur"""
    global PROGRESS
    PROGRESS = 0
    
    data = request.json
    filename = data.get("filename")
    bounds = data.get("bounds")
    selected_items = data.get("params")
    
    temp_path = os.path.join(UPLOAD_FOLDER, "raw_" + filename)
    output_filename = "crop_" + filename
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    if not os.path.exists(temp_path):
        return jsonify({"error": "Erreur d'assemblage : fichier introuvable sur le serveur."}), 400
        
    try:
        fin = pygrib.open(temp_path)
        total_messages = sum(1 for _ in fin)
        fin.close()
        
        if total_messages == 0: total_messages = 1

        fin = pygrib.open(temp_path)
        fout = open(output_path, "wb")
        
        count = 0
        for index, grb in enumerate(fin):
            try:
                name = getattr(grb, 'name', 'Unknown Parameter')
                short_name = getattr(grb, 'shortName', str(getattr(grb, 'parameterNumber', 'unk')))
                param_str = name + " (" + short_name + ")"
            except Exception:
                continue
                
            if param_str in selected_items or len(selected_items) == 0:
                try:
                    sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                    grb.values = sub_data
                    fout.write(grb.tostring())
                    count += 1
                except Exception:
                    fout.write(grb.tostring())
                    count += 1
            
            PROGRESS = int(((index + 1) / total_messages) * 100)
            
        fin.close()
        fout.close()
        
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
        PROGRESS = 100
        return jsonify({"success": True, "download_url": "/api/download/" + output_filename})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(temp_path): os.remove(temp_path)
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
