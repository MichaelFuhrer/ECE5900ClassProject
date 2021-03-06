import keras.models
import numpy as np
import matplotlib.pyplot as plt
import os
import time
import tensorflow as tf
from keras import Input
from keras.models import Sequential
from keras.layers import BatchNormalization, Concatenate, Dense, Dropout, ELU, Normalization
from keras.losses import BinaryCrossentropy
from keras.metrics import BinaryAccuracy, FalseNegatives, FalsePositives, TrueNegatives, TruePositives
from sklearn.manifold import LocallyLinearEmbedding, SpectralEmbedding

# ------------------------------------- MODELS AND DATASET SETUP -------------------------------------

BATCH_SIZE = 128
SHUFFLE_BUFFER_SIZE = 128

feat_size = 2381  # From EMBER feature digest, DO NOT CHANGE!

g_unbatched_feats = None

def prepare_datasets():
    global g_unbatched_feats
    with np.load("dataset/train_set.npz") as train_data:
        train_features = train_data["features"]
        g_unbatched_feats = train_features
        train_labels = train_data["labels"]
        train_dataset = tf.data.Dataset.from_tensor_slices((train_features, train_labels))
    train_dataset = train_dataset.shuffle(SHUFFLE_BUFFER_SIZE).batch(BATCH_SIZE)

    with np.load("dataset/test_set.npz") as test_data:
        test_features = test_data["features"]
        test_labels = test_data["labels"]
        test_dataset = tf.data.Dataset.from_tensor_slices((test_features, test_labels))

    test_dataset = test_dataset.batch(BATCH_SIZE)
    return train_dataset, test_dataset


def make_generator_model():
    model = Sequential([
        Dense(512, activation='linear'),
        ELU(),
        Dropout(0.05),
        Dense(512, activation='sigmoid'),
        Dropout(0.05),
        Dense(feat_size, activation='linear')
    ])
    return model


def make_simple_discriminator_model():
    model = Sequential([
        Dense(512, activation='linear'),
        Normalization(),
        ELU(),
        Dropout(0.05),
        Dense(512, activation='linear'),
        Normalization(),
        ELU(),
        Dropout(0.05),
        Dense(128, activation='linear'),
        Normalization(),
        ELU(),
        Dropout(0.05),
        Dense(1, activation='sigmoid')
    ])
    return model


class SKLearnLLE(keras.layers.Layer):
    classifier = None

    def __init__(self, output_dim, **kwargs):
        self.output_dim = output_dim
        super().__init__(**kwargs)
        self.trainable = False
        self.classifier = LocallyLinearEmbedding(n_neighbors=10, n_components=output_dim)
        self.classifier.fit(g_unbatched_feats[:2000])
        print("LLE Trained")

    def build(self, input_shape):
        self.built = True

    def call(self, x):
        # Eager execution must be enabled
        inp = x.numpy()
        out = self.classifier.fit_transform(inp)
        return tf.convert_to_tensor(out)

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.output_dim


def make_resistant_discriminator_model():
    model = Sequential([
        SKLearnLLE(512),
        Dense(512, activation='linear'),
        Normalization(),
        ELU(),
        Dropout(0.05),
        Dense(512, activation='linear'),
        Normalization(),
        ELU(),
        Dropout(0.05),
        Dense(128, activation='linear'),
        Normalization(),
        ELU(),
        Dropout(0.05),
        Dense(1, activation='sigmoid')
    ])
    return model


def save_model(model, name):
    model_dir = "./models"
    path = os.path.join(model_dir, name)
    model.save(path)


# ------------------------------------------ LOSS FUNCTIONS ------------------------------------------

cross_entropy = BinaryCrossentropy(from_logits=False)


def discriminator_bb_loss(y_hat, d_theta):
    basis = tf.fill(y_hat.get_shape(), 0.5)
    bb_correct = tf.math.greater(y_hat, basis)
    d = tf.where(bb_correct, d_theta, tf.subtract(1, d_theta))
    return -tf.math.reduce_mean(tf.math.log(d))


def discriminator_loss(y_true, y_pred):
    loss = cross_entropy(y_true, y_pred)
    return loss


def generator_loss(y_pred):
    # Assumes that all samples generated by the generator are malware, loss is proportional to
    # how many predictions on generated examples were labeled as benign.
    y_true = tf.zeros_like(y_pred)
    return cross_entropy(y_true, y_pred)


generator_optimizer = tf.keras.optimizers.Adam(1e-4)
discriminator_optimizer = tf.keras.optimizers.Adam(1e-4)

# --------------------------------------- TRAINING AND TESTING ---------------------------------------

EPOCHS = 30
noise_dim = 100


#@tf.function
def discriminator_train_step(samples, discriminator):
    features = samples[0]
    labels = samples[1]

    with tf.GradientTape() as disc_tape:
        pred = discriminator(features, training=True)
        disc_loss = discriminator_loss(labels, pred)

    gradients_of_discriminator = disc_tape.gradient(disc_loss, discriminator.trainable_variables)
    discriminator_optimizer.apply_gradients(zip(gradients_of_discriminator, discriminator.trainable_variables))

    return disc_loss


def discriminator_test_step(samples, discriminator):
    features = samples[0]
    labels = samples[1]

    pred = discriminator(features, training=False)

    ba_m = BinaryAccuracy()
    ba_m.update_state(labels, pred)
    accuracy = ba_m.result().numpy()

    tp_m = TruePositives()
    tp_m.update_state(labels, pred)
    true_positives = tp_m.result().numpy()

    tn_m = TrueNegatives()
    tn_m.update_state(labels, pred)
    true_negatives = tn_m.result().numpy()

    fp_m = FalsePositives()
    fp_m.update_state(labels, pred)
    false_positives = fp_m.result().numpy()
    if (false_positives + true_negatives) == 0:
        false_positive_r = 0
    else:
        false_positive_r = false_positives / (false_positives + true_negatives)

    fn_m = FalseNegatives()
    fn_m.update_state(labels, pred)
    false_negatives = fn_m.result().numpy()
    if (false_negatives + true_positives) == 0:
        false_negative_r = 0
    else:
        false_negative_r = false_negatives / (false_negatives + true_positives)

    return accuracy, false_positive_r, false_negative_r


def gan_test_step(samples, discriminator, generator):
    features = samples[0]
    labels = samples[1]

    malware_i = tf.squeeze(tf.where(labels))
    malware_feats = tf.reshape(tf.cast(tf.gather(features, malware_i), tf.float32), [-1, feat_size])
    benign_i = tf.squeeze(tf.where(labels == 0))
    benign_feats = tf.reshape(tf.cast(tf.gather(features, benign_i), tf.float32), [-1, feat_size])

    noise = tf.random.normal([tf.size(malware_i), noise_dim], dtype=tf.float32)
    generator_input = tf.concat([malware_feats, noise], axis=1)

    gen_output = generator(generator_input, training=False)
    obscured_feats = edit_features(malware_feats, gen_output)

    benign_pred = discriminator(benign_feats, training=False)
    obscured_pred = discriminator(obscured_feats, training=False)

    ba_m = BinaryAccuracy()
    ba_m.update_state(tf.zeros_like(benign_pred), benign_pred)
    ba_m.update_state(tf.ones_like(obscured_pred), obscured_pred)
    accuracy = ba_m.result().numpy()

    tp_m = TruePositives()
    tp_m.update_state(tf.ones_like(obscured_pred), obscured_pred)
    true_positives = tp_m.result().numpy()

    tn_m = TrueNegatives()
    tn_m.update_state(tf.zeros_like(benign_pred), benign_pred)
    true_negatives = tn_m.result().numpy()

    fp_m = FalsePositives()
    fp_m.update_state(tf.zeros_like(benign_pred), benign_pred)
    false_positives = fp_m.result().numpy()
    if (false_positives + true_negatives) == 0:
        false_positive_r = 0
    else:
        false_positive_r = false_positives / (false_positives + true_negatives)

    fn_m = FalseNegatives()
    fn_m.update_state(tf.ones_like(obscured_pred), obscured_pred)
    false_negatives = fn_m.result().numpy()
    if (false_negatives + true_positives) == 0:
        false_negative_r = 0
    else:
        false_negative_r = false_negatives / (false_negatives + true_positives)

    return accuracy, false_positive_r, false_negative_r


@tf.function(experimental_relax_shapes=True)
def edit_features(features: tf.Tensor, generator_output: tf.Tensor):
    assert features.get_shape()[1] == feat_size and generator_output.get_shape()[1] == feat_size
    assert features.get_shape()[0] == generator_output.get_shape()[0]

    # Byte Histogram, 256 Features, Fully modifiable - sum to 1
    byte_hist = tf.linalg.normalize(tf.math.abs(generator_output[:, 0:256]), ord=1, axis=1)[0]
    # Byte Entropy Histogram, 256 Features, Fully modifiable - sum to 1
    byte_entropy_hist = tf.linalg.normalize(tf.math.abs(generator_output[:, 256:512]), ord=1, axis=1)[0]
    # String Extractor, 104 Features
    str_count = tf.reshape(tf.add(features[:, 512], tf.math.abs(generator_output[:, 512])), [-1, 1])  # String count
    str_avg_len = tf.reshape(tf.math.abs(generator_output[:, 513]), [-1, 1])  # Average String Length
    str_hist = tf.abs(generator_output[:, 514:610])  # Non-normalized histogram
    str_printables = tf.reshape(tf.add(features[:, 610], tf.math.abs(generator_output[:, 610])), [-1, 1])  # Printables
    str_entropy = tf.reshape(tf.abs(generator_output[:, 611]), [-1, 1])  # Entropy
    str_other = tf.add(features[:, 612:616], tf.math.abs(generator_output[:, 612:616]))  # Other add-only
    str_extractor = tf.concat([str_count, str_avg_len, str_hist, str_printables, str_entropy, str_other], axis=1)
    # General File Info, 10 Features, Add only
    gen_file_info = tf.add(features[:, 616:626], tf.math.abs(generator_output[:, 616:626]))
    # Header Info, 62 Features
    hdr_timestamp = tf.reshape(tf.math.abs(generator_output[:, 626]), [-1, 1])
    hdr_nonmod = features[:, 627:677]  # Non-modifiable features
    hdr_versions = tf.math.abs(generator_output[:, 677:685])
    hdr_sizes = tf.add(features[:, 685:688], tf.math.abs(generator_output[:, 685:688]))
    hdr_info = tf.concat([hdr_timestamp, hdr_nonmod, hdr_versions, hdr_sizes], axis=1)
    # Section/Imports/Exports Info, 255+1280+128 Features, Non-modifiable
    section_info = features[:, 688:943]
    import_info = features[:, 943:2223]
    export_info = features[:, 2223:2351]
    # Data Directories, 30 Features, Non-modifiable
    data_dir = features[:, 2351:2381]

    obscured_features = tf.concat([byte_hist, byte_entropy_hist, str_extractor, gen_file_info, hdr_info, section_info,
                                   import_info, export_info, data_dir], axis=1)

    return obscured_features


# @tf.function
def gan_train_step(samples, discriminator, generator, black_box=None):
    features = samples[0]
    labels = samples[1]

    malware_i = tf.squeeze(tf.where(labels))
    malware_feats = tf.reshape(tf.cast(tf.gather(features, malware_i), tf.float32), [-1, feat_size])
    noise = tf.random.normal([tf.size(malware_i), noise_dim], dtype=tf.float32)
    generator_input = tf.concat([malware_feats, noise], axis=1)
    with tf.GradientTape() as gen_tape, tf.GradientTape() as disc_tape:
        generator_output = generator(generator_input, training=True)
        obscured_features = edit_features(malware_feats, generator_output)
        if black_box is not None:
            d_theta = discriminator(obscured_features, training=True)
            obscured_pred_bb = black_box(obscured_features, training=False)
            disc_loss = discriminator_bb_loss(obscured_pred_bb, d_theta)
        else:
            pred = discriminator(features, training=True)
            obscured_pred = discriminator(obscured_features, training=True)
            disc_loss = discriminator_loss(labels, pred) + \
                        discriminator_loss(tf.ones_like(obscured_pred), obscured_pred)
        gen_loss = generator_loss(d_theta)

    gradients_of_generator = gen_tape.gradient(gen_loss, generator.trainable_variables)
    gradients_of_discriminator = disc_tape.gradient(disc_loss, discriminator.trainable_variables)
    generator_optimizer.apply_gradients(zip(gradients_of_generator, generator.trainable_variables))
    discriminator_optimizer.apply_gradients(zip(gradients_of_discriminator, discriminator.trainable_variables))

    return gen_loss


def train(dataset, epochs, discriminator, generator=None, black_box=None):
    checkpoint_dir = './training_checkpoints'
    checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
    if generator is None:
        checkpoint = tf.train.Checkpoint(discriminator_optimizer=discriminator_optimizer,
                                         discriminator=discriminator)
    else:
        checkpoint = tf.train.Checkpoint(generator_optimizer=generator_optimizer,
                                         discriminator_optimizer=discriminator_optimizer,
                                         generator=generator,
                                         discriminator=discriminator)

    for epoch in range(epochs):
        start = time.time()
        loss = np.inf
        for batch in dataset:
            if generator is None:
                loss = discriminator_train_step(batch, discriminator)
            else:
                loss = gan_train_step(batch, discriminator, generator, black_box)


        # Save the model every 15 epochs
        if (epoch + 1) % 15 == 0:
            checkpoint.save(file_prefix=checkpoint_prefix)

        print(f'Epoch #{epoch + 1} - Time:{(time.time() - start)}, Loss: {loss}')


def test(dataset, discriminator, generator=None):
    accuracies = []
    fp_rates = []  # False positives
    fn_rates = []  # False negatives
    for batch in dataset:
        if generator is None:
            acc, fp, fn = discriminator_test_step(batch, discriminator)
        else:
            acc, fp, fn = gan_test_step(batch, discriminator, generator)

        accuracies.append(acc)
        fp_rates.append(fp)
        fn_rates.append(fn)



    avg_acc = np.average(accuracies)
    avg_fp_r = np.average(fp_rates)
    avg_fn_r = np.average(fn_rates)

    print(f'Accuracy: {(avg_acc * 100)}%, '
          f'False Positive Rate {(avg_fp_r * 100)}%, '
          f'False Negative Rate {(avg_fn_r * 100)}%')


if __name__ == "__main__":
    train_dataset, test_dataset = prepare_datasets()
    while True:
        user_in = input('Select mode:')

        if user_in == "Simple Discriminator":

            if os.path.exists("./models/simple_disc.model"):
                print("Found model, loading...")
                disc = keras.models.load_model("./models/simple_disc.model")
            else:
                print("Training new model...")
                disc = make_simple_discriminator_model()
                train(train_dataset, EPOCHS, disc)
                save_model(disc, "simple_disc.model")
            print("Testing...")
            test(test_dataset, disc)
            break
        elif user_in == "Simple GAN":
            if os.path.exists("./models/simple_disc.model"):
                print("Found black-box model, loading...")
                disc_bb = keras.models.load_model("./models/simple_disc.model")
                if os.path.exists("./models/simple_generator.model"):
                    print("Found generator model, loading...")
                    gen = keras.models.load_model("./models/simple_generator.model")
                else:
                    print("Training generator...")
                    disc = make_simple_discriminator_model()
                    disc.build([None, feat_size])
                    gen = make_generator_model()
                    train(train_dataset, EPOCHS, disc, gen, disc_bb)
                    save_model(gen, "simple_generator.model")
                print("Testing...")
                test(test_dataset, disc_bb, gen)
                break
            else:
                print("Please train a black-box model first using \"Simple Discriminator\".")
        elif user_in == "Resistant Discriminator":
            if os.path.exists("./models/resistant_disc.model"):
                print("Found model, CANNOT LOAD B/C OF LLE LAYER!")
            #     disc = tf.saved_model.load("./models/resistant_disc.model") LOADING MODEL DOESN'T WORK
            else:
                print("Training new model...")
                disc = make_resistant_discriminator_model()
                train(train_dataset, EPOCHS, disc)
            #     tf.saved_model.save(disc, "./models/resistant_disc.model") SAVING MODEL DOESN'T WORK
            print("Testing...")
            test(test_dataset, disc)
            break
        elif user_in == "Resistant GAN":
            # First train Resistant Discriminator
            print("Training Resistant Discriminator...")
            resistant_disc = make_resistant_discriminator_model()
            train(train_dataset, EPOCHS, resistant_disc)
            print("Testing Resistant Discriminator...")
            test(test_dataset, resistant_disc)
            # Now run adversarial Attack
            print("Training Generator...")
            disc = make_simple_discriminator_model()
            disc.build([None, feat_size])
            gen = make_generator_model()
            train(train_dataset, 15, disc, gen, resistant_disc)
            print("Testing...")
            test(test_dataset, resistant_disc, gen)
            break
        else:
            print("Valid selections are: \"Simple Discriminator\", \"Simple GAN\", \"Resistant Discriminator\", "
                  "and \"Resistant GAN\"")
