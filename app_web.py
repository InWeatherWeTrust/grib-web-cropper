import sys
import os
import json
import shutil
from flask import Flask, render_template, request, jsonify, send_file
from ecmwf.opendata import Client
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
    selected_params = data.get("params")
    selected_levels = data.get("levels")
    selected_steps = data.get("steps")
    
    # Sécurité : trier les échéances sous forme d'entiers
    steps_list = sorted([int(s) for s in selected_steps]) if selected_steps else [12]
    total_steps = len(steps_list)
    
    client = Client()
    
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    # Nettoyage d'un éventuel build précédent cassé
    if os.path.exists(output_path): 
        os.remove(output_path)
    
    try:
        # Ouverture du fichier final unique en mode écriture binaire
        with open(output_path, "wb") as fout:
            
            # BOUCLE ANTI-SATURATION : Traitement individuel de chaque échéance horaire
            for step_index, step_val in enumerate(steps_list):
                PROGRESS = int((step_index / total_steps) * 100)
                
                # Fichier temporaire pour l'échéance en cours
                chunk_raw_filename = f"temp_step_{step_val}.grib2"
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, chunk_raw_filename)
                
                try:
                    # 1. Téléchargement d'une seule échéance (poids plume, aucun freeze)
                    client.retrieve(
                        date=-1,
                        time="00",
                        step=step_val,
                        stream="oper",
                        type="fc",
                        levtype="pl",
                        levelist=selected_levels,
                        param=selected_params,
                        target=chunk_raw_path
                    )
                    
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                    
                    # 2. Découpe géographique immédiate du morceau
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            sub_data, lats, lons = grb.data(
                                lat1=bounds["s"], lat2=bounds["n"], 
                                lon1=bounds["w"], lon2=bounds["e"]
                            )
                            grb.values = sub_data
                            fout.write(grb.tostring())
                        except Exception:
                            fout.write(grb.tostring())
                    fin.close()
                    
                finally:
                    # 3. Nettoyage immédiat du morceau brut pour libérer la RAM de Render
                    if os.path.exists(chunk_raw_path):
                        os.remove(chunk_raw_path)
                        
        PROGRESS = 100
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): 
            os.remove(output_path)
        return jsonify({"error": f"Erreur lors du traitement séquentiel : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
