import sys
import os
import json
import shutil
import urllib.request
import ssl
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(base_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

def get_cep_url(step_val, levtype="sfc"):
    """Calcule l'URL haute disponibilité du CEP Open Data sur AWS"""
    now = datetime.utcnow()
    date_str = now.strftime("%Y%m%d")
    
    # Correction de la nomenclature de l'échéance de l'ECMWF (ex: 012.1)
    if step_val == 0:
        step_str = "000.1"
    else:
        step_str = f"{step_val:03d}1"
    
    if levtype == "pl":
        return f"https://ecmwf.int{date_str}/00z/ifs/0p25/oper/{date_str}0000-{step_str}-oper-pl.grib2"
    else:
        return f"https://ecmwf.int{date_str}/00z/ifs/0p25/oper/{date_str}0000-{step_str}-oper-sfc.grib2"

@app.route('/')
@app.route('/cep')
def index():
    return render_template('cep.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/scan_cep', methods=['POST'])
def scan_cep():
    """Génère la liste des paramètres structurels du modèle européen"""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    if levtype == "pl":
        params = [
            "U component of wind (u)",
            "V component of wind (v)",
            "Geopotential (gh)",
            "Temperature (t)"
        ]
        levels = ["1000 hPa", "850 hPa", "700 hPa", "500 hPa", "300 hPa", "250 hPa"]
    else:
        params = [
            "10m U component of wind (10u)",
            "10m V component of wind (10v)",
            "Mean sea level pressure (msl)",
            "2m temperature (2t)",
            "Total precipitation (tp)"
        ]
        levels = ["0 (surface)"]
        
    return jsonify({"success": True, "params": params, "levels": levels})

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    levtype = data.get("levtype", "sfc")
    selected_params_raw = data.get("params", [])
    selected_levels_raw = data.get("levels", [])
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    selected_params = [p.split("(")[-1].replace(")", "").strip().lower() for p in selected_params_raw if "(" in p]
    
    selected_levels = []
    for l in selected_levels_raw:
        try:
            parts = str(l).strip().split()
            # CORRECTION CRUCIALE : Extraction du premier index textuel de la liste
            if parts and parts[0].isdigit():
                selected_levels.append(int(parts[0]))
        except:
            continue
            
    if levtype == "sfc":
        selected_levels.append(0)
        
    total_steps = len(selected_steps)
    output_filename = "crop_cep_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                url = get_cep_url(step_val, levtype)
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"temp_cep_{step_val}.grib2")
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    req = urllib.request.Request(url)
                    req.add_header('User-Agent', 'Mozilla/5.0')
                    req.add_header('Connection', 'close')
                    with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                except Exception as step_error:
                    print(f"Saut de l'échéance {step_val} : {step_error}")
                    continue
                            
                if os.path.exists(chunk_raw_path) and os.path.getsize(chunk_raw_path) > 0:
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            short_name = str(getattr(grb, 'shortName', '')).lower()
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            lev_type = str(getattr(grb, 'typeOfLevel', '')).lower()
                            
                            match_p = short_name in selected_params
                            if levtype == "sfc":
                                match_l = "surface" in lev_type or "ground" in lev_type or level == 0
                            else:
                                match_l = "isobaric" in lev_type and level in selected_levels
                                
                            if match_p and match_l:
                                # 1. Extraction des points de la zone cible
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                
                                # 2. RECONSTRUCTION ET SYNCHRONISATION DE L'ENTÊTE GEOMETRIQUE (Zéro crash qtVlm)
                                grb.values = sub_data
                                
                                grb['latitudeOfFirstGridPointInDegrees'] = float(bounds["n"])
                                grb['latitudeOfLastGridPointInDegrees'] = float(bounds["s"])
                                grb['longitudeOfFirstGridPointInDegrees'] = float(bounds["w"])
                                grb['longitudeOfLastGridPointInDegrees'] = float(bounds["e"])
                                
                                # CORRECTION DES INDEX DE SHAPE (Colonnes et Lignes de la sous-grille)
                                grb['Ni'] = int(sub_data.shape[1])
                                grb['Nj'] = int(sub_data.shape[0])
                                
                                fout.write(grb.tostring())
                        except Exception as msg_err:
                            print(f"Erreur d'écriture message : {msg_err}")
                            continue
                    fin.close()
                    os.remove(chunk_raw_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite."}), 400
            
        return jsonify({"success": True, "download_url": f"/cep/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur de traitement géospatial : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
