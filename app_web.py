import sys
import os
import json
import ssl
import urllib.request
import base64
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

app = Flask(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0
MAP_BASE64_DATA = ""

def fetch_map_to_base64():
    """Télécharge l'image HD en mémoire et la convertit en texte Base64 pour contourner les bugs de fichiers de Render"""
    global MAP_BASE64_DATA
    try:
        ctx = ssl._create_unverified_context()
        url = "https://wikimedia.org"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        
        with urllib.request.urlopen(req, context=ctx) as response:
            binary_data = response.read()
            # Encodage en texte Base64 propre
            encoded = base64.b64encode(binary_data).decode('utf-8')
            MAP_BASE64_DATA = f"data:image/jpeg;base64,{encoded}"
        print("✅ Fond de carte HD converti en mémoire avec succès !")
    except Exception as e:
        print(f"⚠️ Erreur conversion mémoire carte : {e}")
        # En cas de coupure réseau, secours vectoriel léger pour ne pas laisser l'écran noir
        MAP_BASE64_DATA = "data:image/svg+xml;utf8,<svg xmlns='http://w3.org' viewBox='0 0 360 180'><rect width='360' height='180' fill='%230f172a'/><path d='M165,35 L190,38 L188,44 L178,48 L162,50 L158,53 L160,44 Z' fill='%23334155'/></svg>"

# Téléchargement initial en mémoire
fetch_map_to_base64()

@app.route('/')
def index():
    global MAP_BASE64_DATA
    if not MAP_BASE64_DATA or "jpeg" not in MAP_BASE64_DATA:
        fetch_map_to_base64()
    # Injection directe de la carte sous forme de variable textuelle dans le HTML
    return render_template('index.html', map_data_raw=MAP_BASE64_DATA)

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/crop_fast', methods=['POST'])
def crop_fast_grib():
    global PROGRESS
    PROGRESS = 0
    
    if 'file' not in request.files:
        return jsonify({"error": "Fichier manquant"}), 400
        
    file = request.files['file']
    bounds = json.loads(request.form.get("bounds", "{}"))
    selected_items = json.loads(request.form.get("params", "[]"))
    
    temp_path = os.path.join(UPLOAD_FOLDER, "raw_" + file.filename)
    file.save(temp_path)
    
    output_filename = "crop_" + file.filename
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
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
