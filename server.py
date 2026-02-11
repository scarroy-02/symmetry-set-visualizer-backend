"""
Extended Persistence Server using GUDHI
Flask server that computes extended persistence for radial filtration on closed curves.

To run locally:
    pip install flask flask-cors gudhi numpy
    python persistence_server.py

The server will run on http://localhost:5000
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import numpy as np
import gudhi as gd

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes


def build_simplex_tree(coords, distances, curve_groups):
    """Build a simplex tree for the curve with given distances."""
    st = gd.SimplexTree()
    n = len(coords)
    
    # Insert vertices
    for i in range(n):
        st.insert([i], filtration=float(distances[i]))
    
    # Insert edges (closed loops for each curve)
    for cid, indices in curve_groups.items():
        m = len(indices)
        for j in range(m):
            v1 = indices[j]
            v2 = indices[(j + 1) % m]
            f_val = float(max(distances[v1], distances[v2]))
            st.insert([v1, v2], filtration=f_val)
    
    return st


def process_extended_persistence(st, infinity_cap):
    """Compute extended persistence and return categorized diagrams."""
    st.extend_filtration()
    dgms = st.extended_persistence()
    
    # dgms[0] -> Ordinary, dgms[1] -> Relative, dgms[2] -> Extended+, dgms[3] -> Extended-
    ord0, ord1, rel0, rel1, ext0, ext1 = [], [], [], [], [], []
    
    for dgm_idx, dgm in enumerate(dgms):
        for dim, (birth, death) in dgm:
            is_inf = bool(np.isinf(death))
            d = float(death) if not is_inf else infinity_cap
            b = float(birth)
            
            if dgm_idx == 0:  # Ordinary
                (ord0 if dim == 0 else ord1).append([b, d])
            elif dgm_idx == 1:  # Relative
                (rel0 if dim == 0 else rel1).append([b, d])
            else:  # Extended+ (2) and Extended- (3)
                (ext0 if dim == 0 else ext1).append([b, d])
    
    return ord0, ord1, rel0, rel1, ext0, ext1


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'gudhi_version': gd.__version__})


@app.route('/persistence', methods=['POST'])
def compute_persistence():
    """Compute extended persistence for a single center."""
    try:
        data = request.get_json()
        
        center = data['center']
        points = data['points']
        use_squared = data.get('use_squared_distance', True)
        
        cx, cy = center['x'], center['y']
        n = len(points)
        
        if n < 3:
            return jsonify({'error': 'Need at least 3 points'}), 400
        
        # Group points by curve ID
        curve_groups = {}
        for i, pt in enumerate(points):
            cid = pt.get('curveId', 0)
            if cid not in curve_groups:
                curve_groups[cid] = []
            curve_groups[cid].append(i)
        
        # Compute distances using numpy
        coords = np.array([[p['x'], p['y']] for p in points])
        center_arr = np.array([cx, cy])
        
        if use_squared:
            distances = np.sum((coords - center_arr)**2, axis=1)
        else:
            distances = np.linalg.norm(coords - center_arr, axis=1)
        
        r_min = float(np.min(distances))
        r_max = float(np.max(distances))
        infinity_cap = r_max * 1.5
        
        # Build simplex tree and compute
        st = build_simplex_tree(coords, distances, curve_groups)
        ord0, ord1, rel0, rel1, ext0, ext1 = process_extended_persistence(st, infinity_cap)
        
        return jsonify({
            'ord0': ord0, 'ord1': ord1,
            'rel0': rel0, 'rel1': rel1,
            'ext0': ext0, 'ext1': ext1,
            'r_min': r_min, 'r_max': r_max
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/vineyard', methods=['POST'])
def compute_vineyard():
    """Compute vineyard (persistence over multiple centers)."""
    try:
        data = request.get_json()
        
        centers = data['centers']
        points = data['points']
        use_squared = data.get('use_squared_distance', True)
        
        n = len(points)
        num_centers = len(centers)
        
        if n < 3:
            return jsonify({'error': 'Need at least 3 points'}), 400
        
        # Pre-extract data
        coords = np.array([[p['x'], p['y']] for p in points])
        centers_arr = np.array([[c['x'], c['y']] for c in centers])
        
        # Group points by curve ID
        curve_groups = {}
        for i, pt in enumerate(points):
            cid = pt.get('curveId', 0)
            if cid not in curve_groups:
                curve_groups[cid] = []
            curve_groups[cid].append(i)
        
        # Compute all distances at once (vectorized)
        # Shape: (num_centers, n)
        diff = coords[np.newaxis, :, :] - centers_arr[:, np.newaxis, :]
        if use_squared:
            all_distances = np.sum(diff**2, axis=2)
        else:
            all_distances = np.linalg.norm(diff, axis=2)
        
        max_dist_global = float(np.max(all_distances))
        infinityY = max_dist_global * 1.15
        
        # Results
        ord0_all, ord1_all = [], []
        rel0_all, rel1_all = [], []
        ext0_all, ext1_all = [], []
        
        for ci in range(num_centers):
            distances = all_distances[ci]
            
            # Build simplex tree
            st = build_simplex_tree(coords, distances, curve_groups)
            
            # Compute extended persistence
            st.extend_filtration()
            dgms = st.extended_persistence()
            
            # Process diagrams
            for dgm_idx, dgm in enumerate(dgms):
                for dim, (birth, death) in dgm:
                    is_inf = bool(np.isinf(death))
                    entry = {
                        'birth': float(birth),
                        'death': float(death) if not is_inf else infinityY,
                        'centerIdx': ci,
                        'isInfinite': is_inf,
                        'type': ['ord', 'rel', 'ext', 'ext'][dgm_idx]
                    }
                    
                    if dgm_idx == 0:  # Ordinary
                        (ord0_all if dim == 0 else ord1_all).append(entry)
                    elif dgm_idx == 1:  # Relative
                        (rel0_all if dim == 0 else rel1_all).append(entry)
                    else:  # Extended
                        (ext0_all if dim == 0 else ext1_all).append(entry)
        
        return jsonify({
            'ord0': ord0_all, 'ord1': ord1_all,
            'rel0': rel0_all, 'rel1': rel1_all,
            'ext0': ext0_all, 'ext1': ext1_all,
            'infinityY': infinityY
        })
        
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


if __name__ == '__main__':
    print("Starting Extended Persistence Server...")
    print("Endpoints:")
    print("  GET  /health     - Health check")
    print("  POST /persistence - Single center persistence")
    print("  POST /vineyard    - Vineyard computation")
    print()
    app.run(host='0.0.0.0', port=5000, debug=False)