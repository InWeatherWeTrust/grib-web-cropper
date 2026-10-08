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
        return jsonify({"success": True, "message": "Stockage cloud nettoyé avec succès !"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    selected_params = data.get("params")  # Ex: ['u', 'v']
    selected_levels = [int(l) for l in data.get("levels", [])]  # Ex: [1000, 850]
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    total_steps = len(selected_steps)
    if total_steps == 0:
        return jsonify({"error": "Veuillez sélectionner au moins une échéance."}), 400
        
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    if os.path.exists(output_path): 
        os.remove(output_path)
        
    # Calcul dynamique de la date d'hier au format AAAAMMJJ (Run stable 100% archivé)
    yesterday = datetime.now() - timedelta(days=1)
    date_folder = yesterday.strftime("%Y%m%dd") # Dossier avec 'd' (ex: 20261007d)
    date_file = yesterday.strftime("%Y%m%d")    # Fichier sans 'd' (ex: 20261007)
    
    try:
        ctx = ssl._create_unverified_context()
        
        with open(output_path, "wb") as fout:
            # Traitement séquentiel direct par URL
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                # Formatage de l'échéance à 3 chiffres (ex: 12 -> 012, 24 -> 024)
                step_str = f"{step_val:03d}"
                
                # VÉRITABLE URL DIRECTE DU SERVEUR DE STOCKAGE BRUT DE L'ECMWF (Modèle IFS 0.25° PL)
                url = f"https://ecmwf.int{date_folder}/00z/ifs/0p25/oper/{date_file}0000-{step_str}h-oper-pl.grib2"
                
                chunk_raw_filename = f"temp_step_{step_val}.grib2"
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, chunk_raw_filename)
                
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    # Téléchargement HTTP direct ultra-rapide (Bypass complet de la file d'attente)
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                            
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                        
                    # Filtrage et découpe immédiate par pygrib
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            short_name = getattr(grb, 'shortName', '')
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            
                            param_match = short_name in selected_params if selected_params else True
                            level_match = level in selected_levels if selected_levels else True
                            
                            if param_match and level_match:
                                sub_data, lats, lons = grb.data(
                                    lat1=bounds["s"], lat2=bounds["n"], 
                                    lon1=bounds["w"], lon2=bounds["e"]
                                )
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    
                except Exception as e:
                    print(f"Erreur sur le pas {step_val}: {e}")
                    continue
                finally:
                    if os.path.exists(chunk_raw_path):
                        os.remove(chunk_raw_path)
                        
        PROGRESS = 100
        
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite."}), 400
            
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur lors du traitement direct : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
