from analyze.analyze import extract_mg_value
import re
import tensorflow as tf
from keras import layers
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.preprocessing import LabelEncoder
from keras.utils import to_categorical
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_class_weight
from sklearn.model_selection import GroupShuffleSplit, LeaveOneGroupOut
import numpy as np

def group_data(data, mode='scanner'):
    gd = {}
    if mode == 'scanner':
        # Extract the first two characters and map them to unique integers
        unique_groups = data['SeriesDescription'].apply(lambda x: x[:2]).unique()
        group_map = {group: i for i, group in enumerate(unique_groups)}
        gd['group_id'] = data['SeriesDescription'].apply(lambda x: group_map[x[:2]])
    elif mode == 'repetition':
        # Extract the base part excluding the numeric suffix and map them to unique integers
        def extract_base(description):
            base = re.match(r"(.+?)_(IR|FBP)(\s-\s#\d+)$", description)
            if base:
                return base.group(1).strip()
            return description
        
        gd['base'] = data['SeriesDescription'].apply(extract_base)
        unique_bases = gd['base'].unique()
        base_map = {base: i for i, base in enumerate(unique_bases)}
        gd['group_id'] = gd['base'].apply(lambda x: base_map[x])
    
    return np.array(gd['group_id'])

def load_csv(file_path, label_type='roi_small',mg_filter=None):
    print(f'Loading data from {file_path}')
    data = pd.read_csv(file_path)
    
    print("mg filter",mg_filter)
    print(f'Length of data before filtering: {len(data)}')
    datad = {}
    datad['mg_value'] = data['SeriesDescription'].apply(extract_mg_value)
    if mg_filter is not None:
        data = data[datad['mg_value'] == mg_filter]
        print(f'Length of data after filtering: {len(data)}')

    print("Grouping data...")
    if label_type == 'scanner':
        groups = group_data(data, mode='repetition')
    else:
        groups = group_data(data)
    print(f'Found {len(np.unique(groups))} unique groups')

    # Standardize ROI labels
    if label_type == 'roi_small':
        data['ROI'] = data['ROI'].str.replace(r'\d+', '', regex=True)
        labels = data['ROI'].values
    elif label_type == 'roi_large':
        data['ROI'] = data['ROI']
        labels = data['ROI'].values
    elif label_type == 'scanner':
        labels = data['SeriesDescription'].str[:2].values
    
    print(f'Found {len(np.unique(labels))} unique labels for {label_type}')
    print(f'Labeled classes: {np.unique(labels)} for {label_type}')
    
    features = data.drop(columns=['StudyInstanceUID', 'SeriesNumber', 'SeriesDescription', 'ROI','ManufacturerModelName','Manufacturer','SliceThickness','SpacingBetweenSlices'],errors='ignore')
    if 'deepfeatures' in data.columns:
        features = features['deepfeatures'].apply(eval).apply(pd.Series)
    features = features.values
    
    print(f'Loaded {len(features)} samples with {len(features[0])} features')
    
    return features, labels,groups

def load_data(file_path,one_hot=True, label_type='roi_small',mg_filter=None):
    scaler = StandardScaler()
    
    features, labels,groups = load_csv(file_path, label_type=label_type,mg_filter=mg_filter)
    features = scaler.fit_transform(features)
    class_weights = compute_class_weight('balanced', classes=np.unique(labels), y=labels)
    class_weights = dict(enumerate(class_weights))
    
    label_encoder = LabelEncoder()
    labels = label_encoder.fit_transform(labels)
    if one_hot:
        labels = to_categorical(labels)
    print(f'Found {len(np.unique(labels))} unique labels')
    print(f'Labeled classes: {label_encoder.classes_}')
    classes_size = len(label_encoder.classes_)
    
    #leave one groupe out (do it as much times as there is scanner(loop is probably BEFORE this call))
    #test will be the group leavead out and train will be the train from splits
    
    
    return features, labels, groups, class_weights, classes_size

def define_classifier(input_size,classes_size):
    def mlp(x, dropout_rate, hidden_units):
        for units in hidden_units:
            x = layers.Dense(units, activation=tf.nn.gelu)(x)
            x = layers.Dropout(dropout_rate)(x)
        return x

    input = tf.keras.Input(shape=(input_size,))
    ff = mlp(input, 0.2, [100,60, 30])
    classif = layers.Dense(classes_size, activation='softmax')(ff)

    classifier = tf.keras.Model(inputs=input, outputs=classif)
    optimizer = tf.keras.optimizers.Adam(learning_rate=1e-3)
    classifier.compile(optimizer=optimizer, loss='categorical_crossentropy', metrics=['accuracy'])
    
    classifier.summary()
    print(classifier.summary())
    
    return classifier

def save_classifier_performance(history):
    plt.plot(history.history['accuracy'])
    plt.plot(history.history['val_accuracy'])
    plt.title('Model accuracy')
    plt.ylabel('Accuracy')
    plt.xlabel('Epoch')
    plt.legend(['Train', 'Test'], loc='upper left')
    plt.savefig('accuracy.png')
    plt.close()

    
    
def train_mlp(input_size, data_path, output_path='classifier.h5', classif_type='roi_small', mg_filter=None):
    features, labels, groups, cw, classes_size = load_data(data_path, label_type=classif_type, mg_filter=mg_filter)
    
    mean_val_accuracy = 0
    min_accuracy = 1
    max_accuracy = 0
    
    logo = LeaveOneGroupOut()

    results = {}

    # Iterate over each group to be used as the test set
    for test_index, (train_index, test_index) in enumerate(logo.split(features, labels, groups)):
        X_train_all, X_test = features[train_index], features[test_index]
        y_train_all, y_test = labels[train_index], labels[test_index]
        groups_train_all = groups[train_index]
        unique_train_groups = np.unique(groups_train_all)
        for N in range(1, len(unique_train_groups)):
            splits = GroupShuffleSplit(n_splits=1, train_size=N, random_state=42)
            for train_indices, _ in splits.split(X_train_all, y_train_all, groups_train_all):
                X_train = X_train_all[train_indices]
                y_train = y_train_all[train_indices]
                
                # Define and train the classifier
                classifier = define_classifier(input_size, classes_size)
                
                history = classifier.fit(
                    X_train, y_train,
                    validation_data=(X_test, y_test),
                    batch_size=64,
                    epochs=60,
                    verbose=2,
                    class_weight=cw
                )
                
                # Save the classifier's performance
                save_classifier_performance(history)
                #classifier.save(output_path)
                
                # Calculate max validation accuracy for the current split
                max_val_accuracy = max(history.history['val_accuracy'])
                mean_val_accuracy += max_val_accuracy
                if max_accuracy < max_val_accuracy:
                    max_accuracy = max_val_accuracy
                if min_accuracy > max_val_accuracy:
                    min_accuracy = max_val_accuracy
                
                # Store the performance
                if N not in results:
                    results[N] = []
                results[N].append(max_val_accuracy)
                
                print(f"Test group: {test_index+1}, Training with N={N} scanners, Accuracy: {max_val_accuracy}")

    # Average the mean validation accuracy
    mean_val_accuracy /= len(results)

    print(f"Final results: Mean accuracy: {mean_val_accuracy}, Min accuracy: {min_accuracy}, Max accuracy: {max_accuracy}")
    save_results_to_csv(results, classif_type=classif_type, mg_filter=mg_filter)

    return mean_val_accuracy, max_accuracy, min_accuracy
 
def save_results_to_csv(results,classif_type='roi_small',mg_filter=None):
    df = pd.DataFrame(results)
    #adding columns
    df['classif_type'] = classif_type
    df['mg_filter'] = mg_filter
    df.to_csv('performance_cross_val.csv', index=False)
    
 
def train_mlp_with_data(x_train, y_train, x_val, y_val, input_size, output_path='classifier.h5'):
    classifier = define_classifier(input_size)
    cw = compute_class_weight('balanced', classes=np.unique(y_train), y=y_train)
    cw = dict(enumerate(cw))
    
    history = classifier.fit(
        x_train, y_train,
        validation_data=(x_val, y_val),
        batch_size=64,
        epochs=70,
        verbose=2,
        class_weight=cw
    )
    save_classifier_performance(history)
    classifier.save(output_path)
    max_val_accuracy = max(history.history['val_accuracy'])
    return max_val_accuracy
    
def main():
    gpus = tf.config.experimental.list_physical_devices('GPU')
    if gpus:
        try:
            # Set memory growth to avoid taking all GPU memory
            for gpu in gpus:
                tf.config.experimental.set_memory_growth(gpu, True)
            tf.config.experimental.set_visible_devices(gpus, 'GPU')
            print(f"Using GPU: {gpus}")
        except RuntimeError as e:
            print(e)
    else:
        print("No GPU found. Using CPU.")
    train_mlp(86, 'data/output/features.csv')
    
if __name__ == '__main__':
    main()
