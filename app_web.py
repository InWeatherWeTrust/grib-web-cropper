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
        return jsonify({"success": True, "message": "Stockage cloud nettoyé avec succès !"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 10
    
    data = request.json
    bounds = data.get("bounds")
    selected_params = data.get("params")  # Ex: ['u', 'v']
    selected_levels = [str(l) for l in data.get("levels", [])]  # L'API ECMWF exige des chaînes pour les niveaux
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    total_steps = len(selected_steps)
    if total_steps == 0:
        return jsonify({"error": "Veuillez sélectionner au moins une échéance."}), 400
        
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    if os.path.exists(output_path): 
        os.remove(output_path)
        
    try:
        # Initialisation unique du client officiel de l'ECMWF
        client = Client(source="ecmwf")
        
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                chunk_raw_filename = f"temp_step_{step_val}.grib2"
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, chunk_raw_filename)
                
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    # L'API officielle va chercher le fichier de manière stable sans erreur 404
                    client.retrieve(
                        date=-1,                # Verrouillage sur le run complet de la veille
                        time="00",              # Run de minuit UTC
                        step=step_val,          # Une seule échéance à la fois pour ne pas saturer la RAM
                        stream="oper",
                        type="fc",
                        levtype="pl",           # Pressure levels
                        levelist=selected_levels,
                        param=selected_params,
                        target=chunk_raw_path
                    )
                    
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                        
                    # Découpe géospatiale immédiate avec pygrib
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
                            continue
                    fin.close()
                    
                except Exception as e:
                    print(f"Erreur sur l'échéance {step_val}: {e}")
                    continue
                finally:
                    if os.path.exists(chunk_raw_path):
                        os.remove(chunk_raw_path)
                        
        PROGRESS = 100
        
        # Sécurité finale : si aucune donnée n'a été écrite, on lève une erreur explicite
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite pour ces critères."}), 400
            
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur lors du traitement : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
