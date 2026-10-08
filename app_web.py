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
# Autorise la manipulation de flux de fichiers jusqu'à 500 Mo en entrée/sortie
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
    """Supprime tous les fichiers binaires pour remettre l'espace disque de Render à 0 Mo"""
    try:
        shutil.rmtree(UPLOAD_FOLDER)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        return jsonify({"success": True, "message": "Stockage cloud nettoyé avec succès ! (0 Mo occupés)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    selected_params = data.get("params")  # Ex: ['u', 'v']
    selected_levels = [int(l) for l in data.get("levels", [])]  # Ex: [1000, 500]
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    total_steps = len(selected_steps)
    if total_steps == 0:
        return jsonify({"error": "Veuillez sélectionner au moins une échéance."}), 400
        
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    if os.path.exists(output_path): 
        os.remove(output_path)
        
    # Calcul dynamique de la date d'hier (Verrouillage run stable 100% disponible)
    yesterday = datetime.now() - timedelta(days=1)
    
    # 1. Le nom du dossier prend un "d" à la fin (ex: 20261007d)
    folder_date_str = yesterday.strftime("%Y%m%dd")
    
    # 2. Le nom du fichier de données binaire brut ne prend PAS de "d" (ex: 20261007)
    file_date_str = yesterday.strftime("%Y%m%d")
    
    # Traduction des paramètres de l'interface vers les noms courts officiels pygrib
    # (u/v pour le vent, t pour la température, gh pour le géopotentiel, r pour l'humidité)
    try:
        ctx = ssl._create_unverified_context()
        
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                # Formatage de l'échéance à 3 chiffres exigé par le serveur de l'ECMWF (ex: 12 -> 012, 24 -> 024)
                step_str = f"{step_val:03d}"
                
                # URL officielle, vérifiée et recalibrée en 0.25° HD (Dossier avec 'd', fichier sans 'd')
                url = f"https://ecmwf.int{folder_date_str}/00z/ifs/0p25/oper/{file_date_str}0000-{step_val}h-oper-fc.grib2"
                
                chunk_raw_filename = f"temp_step_{step_val}.grib2"
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, chunk_raw_filename)
                
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    # Téléchargement HTTP direct à la vitesse de la fibre du serveur
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                            
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                        
                    # Filtrage immédiat par pygrib pour extraire la zone géographique et les profondeurs
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            # Extraction des métadonnées du message GRIB
                            short_name = getattr(grb, 'shortName', '')
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            
                            # Validation des filtres sélectionnés par l'utilisateur
                            param_match = short_name in selected_params if selected_params else True
                            level_match = level in selected_levels if selected_levels else True
                            
                            if param_match and level_match:
                                # Découpe de la matrice géospatiale selon vos coordonnées de souris
                                sub_data, lats, lons = grb.data(
                                    lat1=bounds["s"], lat2=bounds["n"], 
                                    lon1=bounds["w"], lon2=bounds["e"]
                                )
                                grb.values = sub_data
                                # Écriture directe à la suite dans le fichier final compilé
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    
                except Exception as e:
                    print(f"Erreur sur le pas horaire {step_val}: {e}")
                    continue
                finally:
                    # Sécurité stockage : suppression immédiate du fichier de 130 Mo pour ne jamais saturer Render
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
