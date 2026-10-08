import sys
import os
import json
import shutil
import gc
from flask import Flask, render_template, request, jsonify, send_file
import ecmwf.opendata
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
    
    # Sécurité : s'assurer que les échéances sont triées et uniques
    steps_list = sorted(list(set([int(s) for s in selected_steps]))) if selected_steps else []
    total_steps = len(steps_list)
    
    if total_steps == 0:
        return jsonify({"error": "Veuillez sélectionner au moins une échéance."}), 400
        
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    # Nettoyage d'un build précédent
    if os.path.exists(output_path): 
        os.remove(output_path)
    
    try:
        # Ouverture du fichier final d'assemblage
        with open(output_path, "wb") as fout:
            
            # BOUCLE DE SÉCURITÉ : Traitement séquentiel avec isolation réseau totale
            for step_index, step_val in enumerate(steps_list):
                # Mise à jour de la jauge globale avant l'étape
                PROGRESS = int((step_index / total_steps) * 100)
                
                chunk_raw_filename = f"temp_step_{step_val}.grib2"
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, chunk_raw_filename)
                
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    # RECONNEXION FORCÉE : On crée un client NEUF et ISOLÉ à chaque morceau
                    # Cela évite le blocage du "pool" de connexion du pare-feu de l'ECMWF
                    client = ecmwf.opendata.Client()
                    
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
                    
                    # Destruction forcée du client réseau en mémoire immédiate
                    del client
                    gc.collect() # Nettoyeur de mémoire Python agressif
                    
                    if not os.path.exists(chunk_raw_path) or os.path.getsize(chunk_raw_path) == 0:
                        continue
                    
                    # Découpe immédiate de la tranche horaire reçue
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
                    
                except Exception as chunk_err:
                    print(f"Erreur sur le pas {step_val}: {chunk_err}")
                    continue # On passe au suivant si un morceau échoue
                    
                finally:
                    # Nettoyage physique du disque pour Render
                    if os.path.exists(chunk_raw_path):
                        os.remove(chunk_raw_path)
                        
        PROGRESS = 100
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur lors du traitement séquentiel isolé : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
