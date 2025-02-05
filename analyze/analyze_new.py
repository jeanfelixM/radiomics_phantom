import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
from datetime import datetime
import numpy as np
from tqdm import tqdm
from sklearn.metrics import silhouette_score
from matplotlib.lines import Line2D
from sklearn.preprocessing import LabelEncoder
import os
import umap.umap_ as umap

markers = ['o', 's', 'D', '^', 'v', '>', '<', 'p', '*', '+', 'x']
scanners = {'A1' : 'SOMATOM Definition Edge', 'A2':'SOMATOM Definition Flash', 'B1':'SOMATOM X.cite', 'B2':'SOMATOM Edge Plus', 'C1':'iCT 256', 'D1':'Revolution EVO', 'E1':'Aquilion Prime SP', 'E2':'GE MEDICAL SYSTEMS', 'F1':'BrightSpeed S', 'G1':'SOMATOM Definition Edge', 'G2':'SOMATOM Definition Flash', 'H1':'Aquilion', 'H2':'Brilliance 64'}

def extract_mg_value(series_description):
    """
    Extracts the milligram (mg) value from the SeriesDescription column.
    Assumes that the mg value is followed by 'mGy'.
    """
    import re
    # Recherche d'un motif numérique suivi de 'mGy'
    match = re.search(r'(\d+)mGy', series_description)
    print(series_description)
    if match:
        print(f"Match: {match}")
        return int(match.group(1))
    else:
        return None

def extract_rep_number(series_description):
    import re
    # Recherche d'un motif numérique du style #1 ou #9 finissant series_description
    match = re.search(r'#(\d+)$', series_description)
    if match:
        return int(match.group(1))
    else:
        return None
    
def extract_rep_number2(series_description):
    import re
    # Recherche d'un motif numérique du style #1 ou #9 finissant series_description
    match = re.search(r'.*(IR|FBP).*#(\d+)$', series_description)
    if match:
        #print(match.group(1))
        if match.group(1) == 'IR':
            return int(match.group(2)) + 100
        else:
            return int(match.group(2))
    else:
        return None
    
def extract_recontruction(series_description):
    import re
    # Recherche d'un motif du style IR ou FBP
    match = re.search(r'IR|FBP', series_description)
    if match:
        return match.group(0)
    else:
        return None

def generate_advanced_markers(num_required):
    base_markers = ['o', 'X', 'H','s', '*','D', '^', 'v', '>', '<', 'p' ]
    line_styles = ['-', '--', '-.', ':']
    marker_sizes = [3]  
    filled_markers = [True, False]

    generated_markers = []

    for filled in filled_markers:
        for size in marker_sizes:
            for line_style in line_styles:
                    for marker in base_markers:
                        if len(generated_markers) < num_required:
                            generated_markers.append({
                                'marker': marker,
                                'linestyle': line_style,
                                'size': size,
                                'filled': filled
                            })
                        else:
                            return generated_markers
    while len(generated_markers) < num_required:
        #duplicating markers
        generated_markers = generated_markers + generated_markers
        

    return generated_markers

def miniload_data(filepath,fsize=None):
    data = pd.read_csv(filepath)
    try:
        data['deepfeatures'] = data['deepfeatures'].apply(lambda x: np.fromstring(x.strip("[]"), sep=','))
    except:
        print('Deep features not fund trying with pyradiomics.')
    

    features = data.drop(columns=['StudyInstanceUID', 'SeriesNumber', 'SeriesDescription', 'ROI','ManufacturerModelName','Manufacturer','SliceThickness','SpacingBetweenSlices'],errors='ignore')
    #verifier si features est plutot une liste ou un string d'une liste
    if features.columns[0] == 'deepfeatures':
        # problematic_rows = features['deepfeatures'].str.contains('\x00', na=False)
        # features = features[~problematic_rows]
        deepfeatures = np.zeros((len(data), len(features['deepfeatures'][0])))
        for i, row in enumerate(features['deepfeatures']):
            deepfeatures[i] = row
        features = pd.DataFrame(deepfeatures, columns=[f'feature_{i:04d}' for i in range(len(deepfeatures[0]))])
        # features = features['deepfeatures'].apply(eval).apply(pd.Series)
    
    print(f"features shape: {features.shape}")
    
    if fsize is None:
        features = features
    elif fsize > 0:
        features = features.iloc[:, :fsize]
    else:
        features = features.iloc[:, fsize + features.shape[1]:]    
    return data, features

def load_data(filepath, color_mode='roi', mg_filter=None,rep_filter=None,self_load=True,data=None,features=None):
    
    if self_load:
        data,features = miniload_data(filepath)
    
    # Sort both data and features by SeriesDescription
    # Find the indecies of the sorted SeriesDescription
    index = data['SeriesDescription'].sort_values().index
    data = data.loc[index]
    features = features.loc[index]

    if color_mode == 'roi':
        labels = data['ROI']
        supp_info = data[['Manufacturer', 'ManufacturerModelName', 'SeriesDescription']]
        extract_id = lambda x: x.split('_')[0]
        supp_info['SeriesDescription'] = supp_info['SeriesDescription'].apply(extract_id)
        # maping = lambda s: 'FBP' if 'fbp' in s.lower() else 'IR'
        # supp_info['Manufacturer'] =  data['SeriesDescription'].map(maping)
    elif color_mode == 'series_desc':
        # Extraction des deux premiers caractères de la SeriesDescription
        labels = data['SeriesDescription'].str[:2].map(scanners)
        supp_info = data['SeriesNumber']
    if color_mode == 'all':
        labels = data['ROI']
        supp_info = data['SeriesDescription'].str[:2]
    if color_mode == 'manufacturer':
        labels = data['Manufacturer']
        supp_info = data['SeriesNumber']
    if color_mode == 'mg':
        data['mg_value'] = data['SeriesDescription'].apply(extract_mg_value)
        labels = data['mg_value']
        supp_info = data['SeriesNumber']
    if color_mode == 'reconstruction':
        data['reconstruction'] = data['SeriesDescription'].apply(extract_recontruction)
        labels = data['reconstruction']
        supp_info = data['SeriesNumber']
    #if color_mode == 'reconstruction':
    
    print(f"Loaded {len(features)} features with a size of {len(features.columns)}")
    print(f"Loaded {len(labels)} labels")
    data['mg_value'] = data['SeriesDescription'].apply(extract_mg_value)

    if mg_filter is not None:
        print(f"feature length before filter: {len(features)}")
        features = features[data['mg_value'] == mg_filter]
        labels = labels[data['mg_value'] == mg_filter]
        supp_info = supp_info[data['mg_value'] == mg_filter]
        data = data[data['mg_value'] == mg_filter]
        print(f"feature length after filter: {len(features)}")
        
    data['SeriesNumber'] = data['SeriesDescription'].apply(extract_rep_number)
    if rep_filter is not None:
        features = features[data['SeriesNumber'] == rep_filter]
        labels = labels[data['SeriesNumber'] == rep_filter]
        supp_info = supp_info[data['SeriesNumber'] == rep_filter]
        data = data[data['SeriesNumber'] == rep_filter]
    #print(f"Features: {features}")
    print(f"Loaded {len(features)} features with a size of {len(features.columns)}")
    print(f"Loaded {len(labels)} labels")
    print(f"Loaded {len(supp_info)} supp_info")

    return features, labels, supp_info

def features_to_numpy(features):
    try:
        features_array = np.zeros([len(features), len(features['deepfeatures'].iloc[0])])
    except:
        return np.array(features)
    for i, row in enumerate(features['deepfeatures']):
        features_array[i] = row
    return features_array

def perform_pca(features):
    features_array = features_to_numpy(features)
    features_scaled = StandardScaler().fit_transform(features_array)
    pca = PCA(n_components=2)
    principal_components = pca.fit_transform(features_scaled)
    explained_variance = pca.explained_variance_ratio_ * 100  # Convertir en pourcentage
    return principal_components, explained_variance

def perform_tsne(features):
    features_array = features_to_numpy(features)
    features_scaled = StandardScaler().fit_transform(features_array)
    perplexity = min(40, len(features_scaled) - 1)
    tsne_results = TSNE(n_components=2, verbose=1, perplexity=perplexity, n_iter=300).fit_transform(features_scaled)
    return tsne_results

def perform_umap(features):
    features_array = features_to_numpy(features)
    features_scaled = features_array
    # features_scaled = StandardScaler().fit_transform(features_array)
    umap_reducer = umap.UMAP(n_neighbors=15, min_dist=1, n_components=2, random_state=24)
    tsne_results = umap_reducer.fit_transform(features_scaled)
    return tsne_results

def save_silhouette_score(scores_filename, datasetname, color_mode, mg_filter, silhouette_avg):
    if not os.path.exists(scores_filename):
        with open(scores_filename, 'w') as f:
            f.write("dataset,color_mode,mg_filter,silhouette_score\n")
    with open(scores_filename, 'a') as f:
        if isinstance(silhouette_avg, str):
            f.write(f"{datasetname},{color_mode},{mg_filter},{silhouette_avg}\n")
        else:
            f.write(f"{datasetname},{color_mode},{mg_filter},{silhouette_avg:.4f}\n")

# from umap import UMAP
# def perform_umap(features):
#     print("Performing UMAP")
#     umap = UMAP(n_components=2)
#     umap_results = umap.fit_transform(features)
#     return umap_results

def analysis(color_mode='series_desc', mg_filter=None, filepath='../../all_dataset_features/averaged_swin_deepfeatures.csv',datasetname='averaged_swin_deepfeatures',rep_filter=None,data=None,features=None):
    print("Analyzing data...")

    if data is not None and features is not None:
        features, labels, supp_info = load_data(filepath, color_mode, mg_filter=mg_filter,rep_filter=rep_filter,self_load=False,data=data,features=features)
    else:
        features, labels, supp_info = load_data(filepath, color_mode, mg_filter=mg_filter,rep_filter=rep_filter)
    
    labels = labels.replace('metastatsis', 'metastasis')
    labels = labels.replace('Siemens Healthineers', 'SIEMENS')
    labels = labels.replace('Philips', 'PHILIPS')

    supp_info = supp_info.replace('metastatsis', 'metastasis')
    supp_info = supp_info.replace('Siemens Healthineers', 'SIEMENS')
    supp_info = supp_info.replace('Philips', 'PHILIPS')

    tissue = 'cyst1'
    # tissue = 'metastasis'
    # tissue = 'hemangioma'
    # tissue = 'normal1'

    features = features[labels == tissue]
    supp_info = supp_info[labels == tissue]
    labels = labels[labels == tissue]

    #silhouette_avg = silhouette_score(features, labels)
    #print(f'Silhouette Score on the whole feature space: {silhouette_avg:.4f}')

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    scores_filename = f"{filepath.rsplit('/', 1)[0]}/silhouette_scores_{timestamp}.csv"
    #save_silhouette_score(scores_filename, timestamp, datasetname, color_mode, mg_filter, silhouette_avg)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_filename = f"{filepath.rsplit('/', 1)[0]}/results_{timestamp}"

    tsne_results = perform_umap(features)

    markers = ('o', 's', 'D', '^', 'v', 'P', 'P', '*', '+', 'x', 'o', 's', 'D', '^', 'v', 'P', 'P', '*', '+', 'x')
    colors = ('r', 'g', 'b', 'c', 'm', 'y', 'k', 'w')
    
    plt.figure(figsize=(10, 8))

    for i, manufacturer in enumerate(supp_info['Manufacturer'].unique()):
        manufacturer_data = supp_info[supp_info['Manufacturer'] == manufacturer]
        # tsne_results = tsne_results+np.random.rand(*tsne_results.shape)*0.5
        for j, scanner_id in enumerate(manufacturer_data['SeriesDescription'].unique()):
            mask = (supp_info['Manufacturer'] == manufacturer) & (supp_info['SeriesDescription'] == scanner_id)
            model = manufacturer_data[mask]['ManufacturerModelName'].iloc[0]
            plt.scatter(tsne_results[mask, 0], tsne_results[mask, 1], 
                        edgecolor=colors[i], 
                        facecolors='none',  # Make markers empty
                        marker=markers[j], s=100,
                        linewidths=2,  # Adjust line width for visibility
                        label=f'{manufacturer} - {model} - {scanner_id}')
            
    # for i, label in enumerate(unique_labels):
    #     plt.scatter([], [], color=colors(i), label=label, s=50)
    # plt.title(datasetname, fontsize=32)
    #plt.title('t-SNE Results', fontsize=18)
    #plt.xlabel('t-SNE 1')
    #plt.ylabel('t-SNE 2')
    # plt.legend(loc='upper left', bbox_to_anchor=(1.1, 1.0), ncol=1, fontsize=16)
    plt.xticks(fontsize=22)
    plt.yticks(fontsize=22)
    plt.grid(True)
    # if len(unique_labels) > 4:
    #     plt.legend(loc='upper center', bbox_to_anchor=(0.5, 1.2), ncol=3, fontsize=16)
    # else:
    #     print('Legend Skipeed for the paper!')
    plt.tight_layout()
    plt.savefig(f"{datasetname}_{color_mode}_{mg_filter}_{rep_filter}_umapCyst.png")
    
    #plt.show()
    
#only doing the silhouette score analysis
def silhouette_score_analysis(color_mode='series_desc', mg_filter=None, filepath='../../all_dataset_features/averaged_swin_deepfeatures.csv', datasetname='averaged_swin_deepfeatures'):
    print("Analyzing data...")

    features, labels, supp_info = load_data(filepath, color_mode, mg_filter=mg_filter)
    
    # Check number of unique labels
    unique_labels = labels.unique()
    if len(unique_labels) < 2:
        print("Not enough labels to calculate silhouette score.")
        silhouette_avg = -10000
    else:
        silhouette_avg = silhouette_score(features, labels)
        print(f'Silhouette Score on the whole feature space: {silhouette_avg:.4f}')
    
    # Save silhouette score to a CSV file
    scores_filename = "silhouette_scores.csv"
    save_silhouette_score(scores_filename, datasetname, color_mode, mg_filter, silhouette_avg)

def plot_tsne(ax, X, y, title, colors, markersize=2):
    scatter = ax.scatter(X[:, 0], X[:, 1], c=y, cmap=colors, s=markersize)
    #augmenting title font size
    ax.set_title(title, fontsize=20)
    ax.grid(True, linestyle='--', linewidth=0.5)
    
    
    return scatter

# Function to plot combined t-SNE results
def plot_combined_tsne(features_list, labels_list):
    print("Performing t-SNE for all feature sets")
    
    le = LabelEncoder()
    
    tsne_results_list = [perform_tsne(features) for features in features_list]
    #tsne_results_list = [np.random.rand(100,2) for i in range(4)]
    
    encoder_labels = [le.fit_transform(labels) for labels in labels_list]
    #encoder_labels = [np.random.randint(0,6,100) for i in range(4)]
    
    unique_labels = le.classes_
    #unique_labels = ['cyst1', 'metastasis', 'hemangioma', 'normal1', 'cyst2', 'normal2']
    
    unique_labels = np.sort(unique_labels)
    #print(f"Unique labels: {unique_labels}")

    print("Plotting combined umap results")

    fig, axs = plt.subplots(2, 2, figsize=(14, 14))
    
    # Define a colormap using the updated method
    #colors = plt.cm.get_cmap('viridis', len(unique_labels))
    colors = plt.get_cmap('viridis', len(unique_labels))

    scatter1 = plot_tsne(axs[0, 0], tsne_results_list[0], encoder_labels[0], 'Radiomics', colors, markersize=3)
    scatter2 = plot_tsne(axs[0, 1], tsne_results_list[1], encoder_labels[1], 'SwinUNETR Features', colors, markersize=3)
    scatter3 = plot_tsne(axs[1, 0], tsne_results_list[2], encoder_labels[2], 'Shallow CNN Features', colors, markersize=3)
    scatter4 = plot_tsne(axs[1, 1], tsne_results_list[3], encoder_labels[3], 'Contrastive SwinUNETR Features', colors, markersize=3)
    
    # Create a legend below the plots
    handles = [Line2D([0], [0], marker='o', color='w', markerfacecolor=colors(i), markersize=5, label=unique_labels[i]) for i in range(len(unique_labels))]
    fig.legend(handles=handles, loc='upper center', title="Labels", ncol=3, fontsize='x-large')

    plt.subplots_adjust(left=0.05, right=0.95, top=0.95, bottom=0.2, wspace=0.3, hspace=0.3)
    plt.show()

    

def batch_analysis():
    color_modes = ['roi']#,'manufacturer']
    mg_filters = [10]
    fsizes = [None]#, -68,700]
    files_dir = '/home/reza/radiomics_phantom/final_features'
    features_files = [f'{files_dir}/features_pyradiomics_full.csv',
                      f'{files_dir}/features_oscar_full.csv',
                      f'{files_dir}/features_swinunetr_full.csv']

    datasetnames = ['Radiomics Features', 'Shallow CNN Features','SwinUNETR Features']
    # datasetnames = ['Contrastive SwinUNETR Features']
    for features_file in features_files:
        for fs in fsizes:
            print(f'Loading data from {features_file}')
            data, features = miniload_data(features_file,fsize=fs)
            print(f'Loaded {len(features)} features with a size of {len(features.columns)}')
            for mg_filter in mg_filters:
                for color_mode in color_modes:
                    print(f'Analyzing {features_file} with color mode {color_mode} and mg filter {mg_filter}')
                    datasetname = datasetnames[features_files.index(features_file)]
                    # datasetname = f"{datasetname}@{fs}@"
                    analysis(color_mode, mg_filter, features_file, datasetname,rep_filter=None, data=data,features=features)


if __name__ == "__main__":
    batch_analysis()
