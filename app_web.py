import sys
import os
import json
import shutil
import urllib.request
import ssl
from datetime import datetime, timedelta
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
        return jsonify({"success": True, "message": "Stockage cloud vidé avec succès !"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    selected_params = data.get("params")  # Ex: ['u', 'v', 't']
    selected_levels = [int(l) for l in data.get("levels", [])]  # Ex: [1000, 850, 500]
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    total_steps = len(selected_steps)
    if total_steps == 0:
        return jsonify({"error": "Veuillez sélectionner au moins une échéance."}), 400
        
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    if os.path.exists(output_path): 
        os.remove(output_path)
        
    # Calcul dynamique de la date d'hier au format AAAAMMJJ pour l'URL de stockage de l'ECMWF
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%Y%m%dd")
    
    # Dictionnaire de traduction des paramètres simplifiés de l'interface vers les codes officiels GRIB de l'ECMWF
    param_mapping = {
        "u": "131.128",  # u-component of wind
        "v": "132.128",  # v-component of wind
        "t": "130.128",  # Temperature
        "gh": "156.128", # Geopotential height
        "r": "157.128"   # Relative humidity
    }
    
    try:
        ctx = ssl._create_unverified_context()
        
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                # Conversion du format de l'échéance à 3 caractères pour l'URL (ex: 12 -> 012, 24 -> 024)
                step_str = f"{step_val:03d}"
                
                # URL brute de stockage de l'échéance IFS mondiale gratuite de l'ECMWF
                url = f"https://ecmwf.int{date_str}/00z/ifs/0p25/oper/{date_str}0000-{step_str}h-oper-fc.grib2"
                
                chunk_raw_filename = f"temp_step_{step_val}.grib2"
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, chunk_raw_filename)
                
                try:
                    # Téléchargement direct HTTP ultra-rapide (Zéro file d'attente serveur)
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, context=ctx, timeout=20) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                            
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                        
                    # Filtrage et découpe géographique immédiate du fichier binaire complet reçu
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            # 1. Vérification du paramètre (Wind, Temp, etc.)
                            indicator = str(grb.indicatorOfParameter if hasattr(grb, 'indicatorOfParameter') else '')
                            short_name = getattr(grb, 'shortName', '')
                            param_match = any(p in selected_params for p in [short_name])
                            
                            # 2. Vérification de la profondeur (Niveau de pression hPa)
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            level_match = level in selected_levels if selected_levels else True
                            
                            if param_match and level_match:
                                # Découpe de la matrice de données
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    
                except Exception as e:
                    print(f"Erreur téléchargement pas {step_val}: {e}")
                    continue
                finally:
                    if os.path.exists(chunk_raw_path):
                        os.remove(chunk_raw_path)
                        
        PROGRESS = 100
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur lors du traitement séquentiel direct : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
