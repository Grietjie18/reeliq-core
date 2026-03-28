import copernicusmarine

if __name__ == "__main__":
    results = copernicusmarine.describe(contains=["OSTIA"])
    for p in results.products:
        print(p.product_id)
        for d in p.datasets:
            print(f"  -> {d.dataset_id}")
