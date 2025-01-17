import tensorflow as tf
import tensorflow.contrib.seq2seq as seq2seq
from tensorflow.contrib.rnn import LSTMCell, LSTMStateTuple, GRUCell, MultiRNNCell
from tensorflow.contrib.rnn.python.ops.rnn_cell import _linear
from tensorflow.python.ops.rnn_cell_impl import _zero_state_tensors
from tensorflow.python.layers.core import Dense
from configs import model_config


class ChatSeq2SeqModel(object):
	def __init__(self, config, use_lstm=True, forward_only=False, attention=False):
		self.attention = attention

		self.input_vocab_size = config.input_vocab_size
		self.target_vocab_size = config.target_vocab_size
		self.enc_hidden_size = config.enc_hidden_size
		self.enc_num_layers = config.enc_num_layers
		self.dec_hidden_size = config.dec_hidden_size
		self.dec_num_layers = config.dec_num_layers
		self.batch_size = config.batch_size

		self.learning_rate = tf.Variable(float(config.learning_rate), trainable=False)
		self.learning_rate_decay_op = self.learning_rate.assign(
			self.learning_rate * config.learning_rate_decay_factor)
		self.global_step = tf.Variable(0, trainable=False)
		self.max_gradient_norm = config.max_gradient_norm

		self.buckets = config.buckets

		# Create the internal multi-layer cell for our RNN.
		if use_lstm:
			single_cell1 = LSTMCell(self.enc_hidden_size)
			single_cell2 = LSTMCell(self.dec_hidden_size)
		else:
			single_cell1 = GRUCell(self.enc_hidden_size)
			single_cell2 = GRUCell(self.dec_hidden_size)

		enc_cell = MultiRNNCell([single_cell1 for _ in range(self.enc_num_layers)])
		dec_cell = MultiRNNCell([single_cell2 for _ in range(self.dec_num_layers)])

		self.encoder_cell = enc_cell
		self.decoder_cell = dec_cell

		self._make_graph(forward_only)
		self.saver = tf.train.Saver(tf.global_variables())

	def _make_graph(self, forward_only):
		self._init_data()
		self._init_embeddings()
		self._init_simple_encoder()
		self._init_decoder(forward_only)

		if not forward_only:
			self._init_optimizer()

	def _init_data(self):
		""" Everything is time-major """
		self.encoder_inputs = tf.placeholder(shape=(None, None), dtype=tf.int32, name="encoder_inputs")
		self.encoder_inputs_length = tf.placeholder(shape=(None,), dtype=tf.int32, name="encoder_inputs_length")

		self.decoder_inputs = tf.placeholder(shape=(None, None), dtype=tf.int32, name="decoder_inputs")
		self.decoder_inputs_length = tf.placeholder(shape=(None,), dtype=tf.int32, name="decoder_inputs_length")

		# Our targets are decoder inputs shifted by one.
		self.decoder_targets = self.decoder_inputs[1:, :]

	def _init_embeddings(self):
		with tf.variable_scope("embedding") as scope:
			self.enc_embedding_matrix = tf.get_variable(
				name="enc_embedding_matrix",
				shape=[self.input_vocab_size, self.enc_hidden_size],
				initializer=tf.contrib.layers.xavier_initializer(),
				dtype=tf.float32)

			self.dec_embedding_matrix = tf.get_variable(
				name="dec_embedding_matrix",
				shape=[self.target_vocab_size, self.dec_hidden_size],
				initializer=tf.contrib.layers.xavier_initializer(),
				dtype=tf.float32)

			self.encoder_inputs_embedded = tf.nn.embedding_lookup(
				self.enc_embedding_matrix, self.encoder_inputs)

			self.decoder_inputs_embedded = tf.nn.embedding_lookup(
				self.dec_embedding_matrix, self.decoder_inputs)

	def _init_simple_encoder(self):
		with tf.variable_scope("encoder") as scope:
			(self.encoder_outputs, self.encoder_state) = tf.nn.dynamic_rnn(cell=self.encoder_cell,
																		   inputs=self.encoder_inputs_embedded,
																		   sequence_length=self.encoder_inputs_length,
																		   time_major=False, dtype=tf.float32)

	def _init_decoder(self, forward_only):

		try :
			with tf.variable_scope("decoder"):
				self.batch_size = tf.shape(self.encoder_inputs)[0]
				self.attn_mech = tf.contrib.seq2seq.LuongAttention(
					num_units=self.dec_hidden_size,
					memory=self.encoder_outputs,
					memory_sequence_length=self.encoder_inputs_length,
					name='LuongAttention')

				self.dec_cell = tf.contrib.seq2seq.AttentionWrapper(
					cell=self.decoder_cell,
					attention_mechanism=self.attn_mech,
					attention_layer_size=self.dec_hidden_size,
					name='Attention_Wrapper')

				self.out_cell = tf.contrib.rnn.OutputProjectionWrapper(
					self.dec_cell, self.target_vocab_size, reuse=False
				)

				if forward_only:
					# maxium unrollings in current batch = max(dec_sent_len) + 1(GO symbol)
					self.max_dec_len = tf.reduce_max(self.decoder_inputs_length + 1, name='max_dec_len')

					start_tokens = tf.tile(tf.constant([model_config.GO_ID], dtype=tf.int32), [self.batch_size],
										   name='start_tokens')

					self.predict_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
						self.dec_embedding_matrix,
						start_tokens=start_tokens,
						end_token=model_config.EOS_ID)

					self.predict_decoder = tf.contrib.seq2seq.BasicDecoder(
						cell=self.out_cell,
						helper=self.predict_helper,
						initial_state=self.out_cell.zero_state(
							dtype=tf.float32, batch_size=self.batch_size))

					infer_dec_outputs, infer_dec_last_state, _ = tf.contrib.seq2seq.dynamic_decode(
						self.predict_decoder,
						output_time_major=False,
						impute_finished=True,
						maximum_iterations=self.max_dec_len)

					# [batch_size x dec_sentence_length], tf.int32
					self.predictions = tf.identity(infer_dec_outputs.sample_id, name='predictions')
				else:
					# maxium unrollings in current batch = max(dec_sent_len) + 1(GO symbol)
					self.max_dec_len = tf.reduce_max(self.decoder_inputs_length + 1, name='max_dec_len')

					self.out_cell = tf.contrib.rnn.OutputProjectionWrapper(
						self.dec_cell, self.target_vocab_size, reuse=False
					)

					self.training_helper = tf.contrib.seq2seq.TrainingHelper(
						inputs=self.decoder_inputs_embedded,
						sequence_length=self.decoder_inputs_length + 1,
						time_major=False,
						name='training_helper')

					self.training_decoder = tf.contrib.seq2seq.BasicDecoder(
						cell=self.out_cell,
						helper=self.training_helper,
						initial_state=self.out_cell.zero_state(
									dtype=tf.float32, batch_size=self.batch_size))

					self.decoder_outputs, self.decoder_state, _ = tf.contrib.seq2seq.dynamic_decode(
						self.training_decoder,
						output_time_major=False,
						impute_finished=True,
						maximum_iterations=self.max_dec_len)

					# logits: [batch_size x max_dec_len x dec_vocab_size+2]
					self.logits = tf.identity(self.decoder_outputs.rnn_output, name='logits')

					# targets: [batch_size x max_dec_len x dec_vocab_size+2]
					self.targets = tf.slice(self.decoder_inputs, [0, 0], [-1, self.max_dec_len], 'targets')

					# masks: [batch_size x max_dec_len]
					# => ignore outputs after `dec_senquence_length+1` when calculating loss
					self.masks = tf.sequence_mask(self.decoder_inputs_length + 1, self.max_dec_len, dtype=tf.float32, name='masks')

					# internal: `tf.nn.sparse_softmax_cross_entropy_with_logits`
					self.loss = tf.contrib.seq2seq.sequence_loss(
						logits=self.logits,
						targets=self.targets,
						weights=self.masks,
						name='batch_loss')
		except Exception as e :
			print(e)

	def _init_optimizer(self):
		params = tf.trainable_variables()
		self.gradient_norms = []
		self.updates = []
		opt = tf.train.AdamOptimizer(self.learning_rate)
		gradients = tf.gradients(self.loss, params)
		clipped_gradients, norm = tf.clip_by_global_norm(gradients, self.max_gradient_norm)
		self.gradient_norms.append(norm)
		self.updates.append(opt.apply_gradients(zip(clipped_gradients, params), global_step=self.global_step))

	def step(self, session, encoder_inputs, encoder_inputs_length, decoder_inputs, decoder_inputs_length, target_weights, forward_only):
		try :
			input_feed = {
				self.encoder_inputs: encoder_inputs,
				self.encoder_inputs_length: encoder_inputs_length,
				self.decoder_inputs: decoder_inputs,
				self.decoder_inputs_length: decoder_inputs_length
			}

			if forward_only:
				output_feed = [self.predictions]
				prediction = session.run(output_feed, input_feed)
				return prediction
			else:
				output_feed = [self.updates, self.gradient_norms, self.loss, self.encoder_state, self.decoder_state]
				updates, gradient, loss, encoder_embedding, decoder_embedding = session.run(output_feed, input_feed)
				return gradient, loss, None, None, encoder_embedding, decoder_embedding
		except Exception as e :
			print("error")



