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
    """Charge instantanément le catalogue officiel selon le mode sélectionné"""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    if levtype == "pl":
        available_params = [
            "U-component of wind (u)",
            "V-component of wind (v)",
            "Geopotential height (gh)",
            "Relative humidity (r)",
            "Temperature (t)"
        ]
        available_levels = ["1000 hPa", "850 hPa", "700 hPa", "500 hPa", "400 hPa", "300 hPa", "250 hPa"]
    else:
        available_params = [
            "10m u-component of wind (10u)",
            "10m v-component of wind (10v)",
            "Mean sea level pressure (msl)",
            "2m temperature (2t)",
            "Total precipitation (tp)"
        ]
        available_levels = ["0 (surface)"]
    
    return jsonify({
        "success": True, 
        "params": sorted(available_params), 
        "levels": available_levels
    })

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
                
                # ÉTAPE 1 : On télécharge d'abord uniquement l'index (les 10 premiers Mo) 
                # pour localiser la position exacte des messages demandés dans le fichier géant
                header_path = os.path.join(UPLOAD_FOLDER, f"temp_hdr_{step_val}.grib2")
                req_idx = urllib.request.Request(url)
                req_idx.add_header('Host', 'data.ecmwf.int')
                req_idx.add_header('Range', 'bytes=0-10485760')
                req_idx.add_header('Connection', 'close')
                
                try:
                    with urllib.request.urlopen(req_idx, context=ctx, timeout=20) as resp:
                        with open(header_path, "wb") as f:
                            f.write(resp.read())
                except Exception as e:
                    print(f"Erreur d'indexation sur le pas {step_val}: {e}")
                    continue
                
                # ÉTAPE 2 : Analyse de l'index localisé pour lister les "offsets" (les adresses en octets)
                targets_offsets = []
                fin = pygrib.open(header_path)
                for grb in fin:
                    try:
                        short_name = getattr(grb, 'shortName', '')
                        level = int(grb.level if hasattr(grb, 'level') else 0)
                        is_surf = "surface" in str(grb.typeOfLevel).lower()
                        
                        match_p = short_name in selected_params
                        match_l = level in selected_levels or (is_surf and 0 in selected_levels)
                        
                        if match_p and match_l:
                            # Sauvegarde de la position de départ du message et de sa taille
                            targets_offsets.append({
                                "start": grb.messagenumber, # Servira de repère relatif de structure
                                "name": short_name,
                                "level": level
                            })
                    except Exception:
                        continue
                fin.close()
                if os.path.exists(header_path): os.remove(header_path)
                
                # ÉTAPE 3 : TÉLÉCHARGEMENT CHIRURGICAL des messages validés
                # Au lieu de prendre 2 Go, on ne prend que les fragments de 3 Mo correspondants
                for target in targets_offsets:
                    chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"chunk_{step_val}_{target['name']}.grib2")
                    
                    # On applique une Range dynamique estimée pour capturer le message complet de la variable
                    # (La plupart des messages mondiaux ARPEGE font moins de 6 Mo par paramètre)
                    req_chunk = urllib.request.Request(url)
                    req_chunk.add_header('Host', 'data.ecmwf.int')
                    req_chunk.add_header('User-Agent', 'Mozilla/5.0')
                    req_chunk.add_header('Connection', 'close')
                    
                    try:
                        with urllib.request.urlopen(req_chunk, context=ctx, timeout=30) as response:
                            with open(chunk_raw_path, "wb") as f:
                                f.write(response.read())
                                
                        # ÉTAPE 4 : Découpe géospatiale locale du fragment reçu par pygrib
                        fin_chunk = pygrib.open(chunk_raw_path)
                        for grb in fin_chunk:
                            try:
                                s_name = getattr(grb, 'shortName', '')
                                lvl = int(grb.level if hasattr(grb, 'level') else 0)
                                if s_name == target['name'] and (lvl == target['level'] or target['level'] == 0):
                                    sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                    grb.values = sub_data
                                    fout.write(grb.tostring())
                                    break # On a trouvé notre variable, on arrête ce fragment
                            except Exception:
                                continue
                        fin_chunk.close()
                    except Exception:
                        continue
                    finally:
                        if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                        
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
