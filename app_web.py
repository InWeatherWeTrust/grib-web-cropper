import sys
import os
import json
import ssl
import urllib.request
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

# Configuration explicite des dossiers pour Render
current_dir = os.path.dirname(os.path.abspath(__file__))
template_folder = os.path.join(current_dir, 'templates')
static_folder = os.path.join(current_dir, 'static')

app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)

UPLOAD_FOLDER = os.path.join(current_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(static_folder, exist_ok=True)

PROGRESS = 0

def force_download_map():
    """Télécharge l'image mondiale directement dans le dossier static interne du serveur"""
    target_map = os.path.join(static_folder, "world_map.jpg")
    if not os.path.exists(target_map) or os.path.getsize(target_map) < 10000:
        try:
            ctx = ssl._create_unverified_context()
            url = "https://wikimedia.org"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            
            with urllib.request.urlopen(req, context=ctx) as response:
                with open(target_map, "wb") as f:
                    f.write(response.read())
            print("✅ Carte HD installée avec succès dans /static/world_map.jpg")
        except Exception as e:
            print(f"⚠️ Erreur téléchargement carte : {e}")

# Lancement forcé au démarrage du script
force_download_map()

@app.route('/')
def index():
    # Double vérification de sécurité à chaque chargement de page
    force_download_map()
    return render_template('index.html')

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
