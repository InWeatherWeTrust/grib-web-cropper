import sys
import os
import json
import shutil
import urllib.request
import ssl
import tempfile
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

def get_ecmwf_arpege_url(step_val, levtype="sfc"):
    """Calcule l'URL brute exacte selon le mode choisi (Surface ou Altitude hPa)"""
    yesterday = datetime.utcnow() - timedelta(days=1)
    date_str = yesterday.strftime("%Y%m%d")
    step_str = f"{step_val:03d}"
    ecmwf_static_ip = "136.156.129.245"
    
    suffix = "pl" if levtype == "pl" else "fc"
    return f"https://{ecmwf_static_ip}/forecasts/{date_str}/00z/arpege/0p25/oper/{date_str}0000-{step_str}h-oper-{suffix}.grib2"

@app.route('/')
@app.route('/arpege')
def index():
    return render_template('arpege.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/scan_arpege', methods=['POST'])
def scan_arpege():
    """VRAI SCAN DYNAMIQUE : Lit les 40 premiers Mo du fichier GRIB distant pour extraire les vrais paramètres"""
    data = request.json
    selected_steps = data.get("steps",)
    levtype = data.get("levtype", "sfc")
    
    if not selected_steps:
        return jsonify({"error": "Cochez au moins une échéance."}), 400
        
    first_step = sorted([int(s) for s in selected_steps])[0]
    url = get_ecmwf_arpege_url(first_step, levtype)
    
    scan_filename = f"dynamic_scan_{first_step}_{levtype}.grib2"
    scan_path = os.path.join(UPLOAD_FOLDER, scan_filename)
    if os.path.exists(scan_path): os.remove(scan_path)
    
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url)
        req.add_header('Host', 'data.ecmwf.int')
        req.add_header('User-Agent', 'Mozilla/5.0')
        # SÉCURITÉ RAM/TIMEOUT : On ne télécharge que les 40 premiers Mo (Indexation + 10 à 15 couches)
        req.add_header('Range', 'bytes=0-41943040')
        req.add_header('Connection', 'close')
        
        with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
            with open(scan_path, "wb") as f:
                f.write(response.read())
                
        # Lecture et extraction par pygrib de la structure réelle
        fin = pygrib.open(scan_path)
        params_set = set()
        levels_set = set()
        
        for grb in fin:
            try:
                name = getattr(grb, 'name', 'Unknown')
                short_name = getattr(grb, 'shortName', '')
                level = getattr(grb, 'level', 0)
                lev_type = getattr(grb, 'typeOfLevel', 'surface')
                
                if short_name:
                    params_set.add(f"{name} ({short_name})")
                if level is not None:
                    # Stockage propre textuel pour l'interface (ex: "500 hPa" ou "0 (surface)")
                    if "surface" in str(lev_type).lower():
                        levels_set.add(f"0 (surface)")
                    else:
                        levels_set.add(f"{level} hPa")
            except Exception:
                continue
        fin.close()
        
        if os.path.exists(scan_path): os.remove(scan_path)
        
        # Si le fragment partiel n'a rien donné, message d'alerte propre
        if len(params_set) == 0:
            return jsonify({"error": "Le serveur de l'ECMWF n'a renvoyé aucun en-tête lisible à cette adresse."}), 400
            
        return jsonify({
            "success": True, 
            "params": sorted(list(params_set)), 
            "levels": sorted(list(levels_set), key=lambda x: int(x.split()[0]) if x.split()[0].isdigit() else 0)
        })
    except Exception as e:
        if os.path.exists(scan_path): os.remove(scan_path)
        return jsonify({"error": f"Erreur d'analyse réelle du fichier GRIB : {str(e)}"}), 500

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    levtype = data.get("levtype", "sfc")
    selected_params_raw = data.get("params",)
    selected_levels_raw = data.get("levels",)
    selected_steps = sorted(list(set([int(s) for s in data.get("steps",)])))
    
    selected_params = []
    for p in selected_params_raw:
        if "(" in p:
            selected_params.append(p.split("(")[-1].replace(")", "").strip())
        else:
            selected_params.append(p.strip())
            
    selected_levels = []
    for l in selected_levels_raw:
        try:
            selected_levels.append(int(str(l).split()[0].strip()))
        except:
            if "surface" in str(l):
                selected_levels.append(0)
        
    total_steps = len(selected_steps)
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                url = get_ecmwf_arpege_url(step_val, levtype)
                
                req = urllib.request.Request(url)
                req.add_header('Host', 'data.ecmwf.int')
                req.add_header('User-Agent', 'Mozilla/5.0')
                req.add_header('Connection', 'keep-alive')
                
                try:
                    with urllib.request.urlopen(req, context=ctx, timeout=60) as response:
                        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                            shutil.copyfileobj(response, tmp_file)
                            tmp_file_path = tmp_file.name
                        
                        fin = pygrib.open(tmp_file_path)
                        for grb in fin:
                            try:
                                short_name = getattr(grb, 'shortName', '')
                                level = int(grb.level if hasattr(grb, 'level') else 0)
                                is_surf = "surface" in str(grb.typeOfLevel).lower()
                                
                                match_p = short_name in selected_params
                                match_l = level in selected_levels or (is_surf and 0 in selected_levels)
                                
                                if match_p and match_l:
                                    sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                    grb.values = sub_data
                                    fout.write(grb.tostring())
                            except Exception:
                                continue
                        fin.close()
                        if os.path.exists(tmp_file_path): os.remove(tmp_file_path)
                except Exception as step_error:
                    print(f"Erreur sur le pas {step_val}: {step_error}")
                    continue
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite. Vérifiez la validité de vos filtres sur ce fichier."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur de découpe : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
