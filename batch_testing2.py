from mlp_classif2 import train_mlp
from mlp_classif2 import train_mlp_with_data
from mlp_classif2 import load_data
from svm_classif import train_svm
from svm_classif import train_svm_with_data

def update_performance_file(model, scanners, mlp_accuracy, svm_accuracy, output_path,classif_type='roi_small',mlp_max_accu=None,mlp_min_accu=None,svm_max_accu=None,svm_min_accu=None):
    entry = f'''
    {model}_classif_{classif_type}_{scanners}_scanners:
        mlp: 
            mean : {mlp_accuracy:.4f}
            max : {mlp_max_accu:.4f}
            min : {mlp_min_accu:.4f}
        svm: 
            mean: {svm_accuracy:.4f}
            max: {svm_max_accu:.4f}
            min: {svm_min_accu:.4f}
        path : {output_path}
    '''
    with open('performance_classif.txt', 'a') as file:
        file.write(entry)

configurations = {
    'swin_finetune': (3072),
}
classif_types = ['roi_large','scanner']
for classif_type in classif_types:
    for model, info_list in configurations.items():
        latent_size = info_list
        data_path = f'features_{model}.csv'
        #output_path_mlp = f'classif_models/classifier_{model}_{n_scanners}_{classif_type}_{qmg}_mlp.h5'
        mlp_accuracy,mlp_max_accu,mlp_min_accu = train_mlp(latent_size, data_path, "",classif_type,mg_filter=None)
        _,svm_accuracy,svm_max_accu,svm_min_accu = train_svm(data_path,classif_type,mg_filter=None)
        #update_performance_file(model, n_scanners, mlp_accuracy, svm_accuracy, output_path_mlp,classif_type,mlp_max_accu,mlp_min_accu,svm_max_accu,svm_min_accu)
