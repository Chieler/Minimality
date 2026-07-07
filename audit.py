from memory.mem_class import Memory_Manager

def debug_dump_graph(memory_manager):
    print("\n=============================================")
    print("      🔍 GRAPHQLITE COMPREHENSIVE AUDIT      ")
    print("=============================================\n")
    
    # 1. Check basic stats
    if hasattr(memory_manager.G, 'stats'):
        print(f"Graph Stats: {memory_manager.G.stats()}\n")
    
    # 2. Dump all nodes via native Python API
    print("--- 🟢 ALL NODES IN GRAPH ---")
    try:
        nodes = memory_manager.G.get_all_nodes()
        if not nodes:
            print("[Warning] No nodes returned by get_all_nodes()")
        for node in nodes:
            print(f"Node -> {node}")
    except Exception as e:
        print(f"Could not fetch nodes via API: {e}")
        
    print("\n--- 🔵 ALL EDGES IN GRAPH ---")
    # 3. Dump all edges via native Python API
    try:
        edges = memory_manager.G.get_all_edges()
        if not edges:
            print("[Warning] No edges returned by get_all_edges()")
        for edge in edges:
            print(f"Edge -> {edge}")
    except Exception as e:
        print(f"Could not fetch edges via API: {e}")

    print("\n--- 📊 RAW CYAN/CYPHER DUMP ---")
    # 4. Dump everything via a structural match query
    try:
        # graphqlite allows running direct queries on the Graph instance
        raw_nodes = memory_manager.G.query("MATCH (n) RETURN n")
        print(f"Total raw nodes found via Cypher: {len(raw_nodes)}")
        for idx, row in enumerate(raw_nodes):
            print(f"  Row {idx}: {dict(row) if hasattr(row, 'items') else row}")
            
        raw_edges = memory_manager.G.query("MATCH (a)-[r]->(b) RETURN a.name, TYPE(r), b.name")
        print(f"Total raw edges found via Cypher: {len(raw_edges)}")
        for idx, row in enumerate(raw_edges):
            print(f"  Row {idx}: {dict(row) if hasattr(row, 'items') else row}")
    except Exception as e:
        print(f"Cypher fallback query failed: {e}")
        
    print("\n=============================================\n")

# Run the diagnostic tool right after data injection
debug_dump_graph(mm)