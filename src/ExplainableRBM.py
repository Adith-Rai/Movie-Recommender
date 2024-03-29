# -*- coding: utf-8 -*-
import sys
import math
import numpy as np
import matplotlib.pyplot as plt

import tensorflow.compat.v1 as tf
tf.disable_v2_behavior()

from surprise import AlgoBase
from surprise import PredictionImpossible

from MovieSimilarityKNN import MovieSimilarityKNN
from UserSimilarityKNN import UserSimilarityKNN

class ExplainableRBM(AlgoBase):

    def __init__(self, dl, epochs=20, hiddenDimensions=200, learningRate=0.005, batchSize=100, userTopK=40, movieTopK=40):
        AlgoBase.__init__(self)
            
        self.uk = userTopK
        self.mk = movieTopK
        self.epochs = epochs
        self.hiddenDimensions = hiddenDimensions
        self.learningRate = learningRate
        self.batchSize = batchSize
        self.dl = dl
        self.stoplist = self.dl.getStopList()  #just basic words that more specific words can be added to stoplist file
        self.avgUserYear, self.allYears = self.dl.getAvgUserYear()
        self.popular = self.dl.getPopularityRanks()
        self.avgMovieRating = self.dl.getAvgRating()        
        

    def fit(self, trainset, train=True, userID=1):
        AlgoBase.fit(self, trainset)

        #Initialize and Generate Item Similarities
        movieSimKNN = MovieSimilarityKNN(self.dl, k=self.mk)
        movieSimKNN.fit(trainset, train)

        userCount = trainset.n_users
        movieCount = trainset.n_items
        
        #Not using sparse matrix because computing user similarities takes too long with scipy matrix 
        #converting scipy sparse -> numpy array and computing takes too longs as well
        #Used float32 rather than float16 as computations time for float16 was taking too long (operations on modern systems need to be converted to float32 and the result back to float 16)
        trainingMatrix = np.zeros([userCount, movieCount], dtype=np.float32)
        
        for (uid, iid, rating) in trainset.all_ratings():           
            #reduce rating to value between 0 and 1
            trainingMatrix[int(uid), int(iid)] = rating / 5.0

        # Set visible dimentions to number of unique movies (same for every user, rated or not)
        self.visibleDimensions = trainingMatrix.shape[1]
        
        # Create and run RBM with (num items * rating values) visible nodes for training this model
        self.MakeGraph()
        
        self.sess = tf.Session()       

        #Fill User Ratings Matrix with Movie Content Based Predicted Ratings to make Dense Training Matrix
        #User Merged Dense Training Matrix Compute User Similarities with KNN
        userSimKNN = UserSimilarityKNN(self.dl, k=self.uk)
        if train:
            self.densetrainingMatrix =  movieSimKNN.mergeMovieSimilaritiesUserRatings(trainingMatrix)
            self.userSimilarityRatings = userSimKNN.computeUserKNNAlgorithm(trainingMatrix, self.densetrainingMatrix)

        else:
            self.load()
            if (self.userSimilarityRatings.shape!=trainingMatrix.shape) or (self.densetrainingMatrix.shape!=trainingMatrix.shape):
                print("PLEASE RE-TRAIN MODEL")
                sys.exit() 
            #Modify only the row with userID passed
            self.densetrainingMatrix[trainset.to_inner_uid(str(userID))] =  (movieSimKNN.mergeMovieSimilaritiesUserRatings(trainingMatrix, trainset.to_inner_uid(str(userID))))[trainset.to_inner_uid(str(userID))]
            self.userSimilarityRatings[trainset.to_inner_uid(str(userID))] = (userSimKNN.computeUserKNNAlgorithm(trainingMatrix, self.densetrainingMatrix, trainset.to_inner_uid(str(userID))))[trainset.to_inner_uid(str(userID))]

        
        print("\nInitializing Session...\n")
        
        self.sess.run(tf.global_variables_initializer()) 

        if train:
            loss = []
            sloss = []
            print("\nTraining Explainable RBM...")
            for epoch in range(self.epochs):

                trY = np.array(self.userSimilarityRatings)
                for i in range(0, trY.shape[0], self.batchSize):
                    self.sess.run(self.update, feed_dict={self.Y: trY[i:i+self.batchSize]})

                print("\nTrained epoch ", epoch)
                sloss.append(self.sess.run(self.sloss_sum, feed_dict={ self.Y: trY}))
                print("Similarity Loss: ", sloss[-1])
                
            plt.plot(loss, 'r')
            plt.plot(sloss, 'b')
            plt.ylabel('RBM Loss')
            plt.xlabel('Epochs')    
            plt.show()
            print("\nTrained Explainable RBM.")
            
            self.save()
            
        print("\nComputing User Ratings from Trained Explainable RBM...")

        #Get the ratings for all movies for each user using the learned waits from above
        #The trained waits are passed forward, then a backward pass is made to determine the unknown ratings
        self.predictedRatings = np.zeros([userCount, movieCount], dtype=np.float32)
        
        
        
        if False:
            #Compute recomendations for every user
            for uiid in range(trainset.n_users):
            
                if (uiid % 50 == 0):
                    print("Processing user  " + str(uiid) + "  of  " + str(userCount))
            
                self.computeUserRecommendation(uiid)
        else:
            #Compute recomendations for only the single selected user
            self.computeUserRecommendation(trainset.to_inner_uid(str(userID)))
           

        print("\nPredicted User Ratings Computed using Exlpainable RBM")
        
        return self
        

 
    def MakeGraph(self):

        tf.set_random_seed(0)
        
        # Create variables for the graph, weights and biases
        self.Y = tf.placeholder(tf.float32, [None, self.visibleDimensions], name="Y")
        
        # Initialize weights as zero
        self.simWeights = tf.Variable(tf.zeros([self.visibleDimensions, self.hiddenDimensions], tf.float32, name="similarityWeights"))
        
        #Initialize biases for hidden layer and visible layer (similarity and movies)
        self.hiddenBias = tf.Variable(tf.zeros([self.hiddenDimensions], tf.float32, name="hiddenBias"))
        self.visibleBias = tf.Variable(tf.zeros([self.visibleDimensions], tf.float32, name="visibleBias"))
        self.simBias = tf.Variable(tf.zeros([self.visibleDimensions], tf.float32, name="similarityBias"))
        
        # Forward pass for movies and similarities
        # Compute hidden layer
        hProb0 = tf.nn.sigmoid(tf.matmul(self.Y, self.simWeights) + self.hiddenBias)
        # Sample from all of the distributions
        hSample = tf.nn.relu(tf.sign(hProb0 - tf.random_uniform(tf.shape(hProb0))))
        sforward = tf.matmul(tf.transpose(self.Y), hSample)
        
        # Backward pass
        # Stitch it together to define the backward pass and updated hidden biases
        sProb = tf.nn.sigmoid(tf.matmul(hSample, tf.transpose(self.simWeights)) + self.simBias)
        sSample = tf.nn.relu(tf.sign(sProb - tf.random_uniform(tf.shape(sProb))))
        hProb1 = tf.nn.sigmoid(tf.matmul(sSample, self.simWeights) + self.hiddenBias)
        sbackward = tf.matmul(tf.transpose(sSample), hProb1)
    
        # Update weights and biases
        # Update the weights by learning rate using the Contrastive divergence (forward - backward)
        simWeightUpdate = self.simWeights.assign_add( (self.learningRate * (sforward - sbackward) / tf.to_float(tf.shape(self.Y)[0])))
        # Update hidden bias, to reduce error between visible and hidden
        hiddenBiasUpdate = self.hiddenBias.assign_add( (self.learningRate * tf.reduce_mean(hSample - hProb1, 0)))
        # Update the visible/movie and similarity biases, to reduce error between visible and hidden
        simBiasUpdate = self.simBias.assign_add( (self.learningRate * tf.reduce_mean(self.Y - sSample, 0)))

        # Calculate Loss (RMSE)
        sloss = (self.Y - sSample)
        self.sloss_sum = tf.sqrt(tf.reduce_mean(sloss**2))

        self.update = [hiddenBiasUpdate, simWeightUpdate, simBiasUpdate]    
 
 
   
    def computeUserRecommendation(self, uiid):
        
        thisUserID = int(self.trainset.to_raw_uid(uiid))

        hidden = tf.nn.sigmoid(tf.matmul(self.Y, self.simWeights) + self.hiddenBias)
        similar = tf.nn.sigmoid(tf.matmul(hidden, tf.transpose(self.simWeights)) + self.simBias)
        #visible = tf.nn.sigmoid(tf.matmul(hidden, tf.transpose(self.weights)) + self.visibleBias)

        feed = self.sess.run(hidden, feed_dict={ self.Y: [self.userSimilarityRatings[uiid]]} )
        srec = self.sess.run(similar, feed_dict={ hidden: feed } )
        #vrec = self.sess.run(visible, feed_dict={ hidden: feed } )
            
        #Average of movie rating predictions from movie ratings ###and similar users movie ratings
        recs = srec[0]#(srec[0] + vrec[0])/2.0
            
            
        #Arrange the reccomendations in order of compatability to user
        for itemID, rec in enumerate(recs):

            predRating = rec
                   
            thisMovieID = int(self.trainset.to_raw_iid(itemID))
            ageWeight = self.computeYearSimilarity(self.avgUserYear[thisUserID][0], self.allYears[thisMovieID], self.avgUserYear[thisUserID][1])
                
            #90% Predicted rating 10% Distance from median year of movies watched
            predRatingRelavance = predRating*0.90 + ageWeight*0.10
                
            popularityWeight = 1-1.0*self.popular[thisMovieID]/len(self.popular)
                
            avgRating = self.avgMovieRating[thisMovieID]*0.2
                
            #average rating and popularity
            ratingPopularityWeight = avgRating * popularityWeight
                
            #95% based on predicted rating and year 5% based on average rating and popularity
            ratingAssigned = (0.95*predRatingRelavance + 0.05*ratingPopularityWeight)*5.0
                
            self.predictedRatings[uiid, itemID] = ratingAssigned
        
    
    def estimate(self, u, i):

        if not (self.trainset.knows_user(u) and self.trainset.knows_item(i)):
            raise PredictionImpossible('User and/or item is unkown.')
        
        return self.predictedRatings[u, i]
        
    
    def computeYearSimilarity(self, years1, years2, stDev):
        
        diff = years1 - years2
        if stDev == 0:
            stDev = 1
        
        if diff > 0:
            sim = math.exp(-diff / (stDev*18.0))
        else:
            sim = math.exp(diff / (stDev*51.0))
            
        return sim
    
    
    def save(self):
        saveWeights = self.weights.eval(session=self.sess)
        saveSimWeights = self.simWeights.eval(session=self.sess)
        saveVB = self.visibleBias.eval(session=self.sess)
        saveSB = self.simBias.eval(session=self.sess)
        saveHB = self.hiddenBias.eval(session=self.sess)
        toWrite = [saveWeights, saveVB, saveHB, self.userSimilarityRatings, saveSimWeights, saveSB, self.densetrainingMatrix]
        self.dl.writeLearnedWeightsToFile(rbm=toWrite)
        
    def load(self):
        loadedWeights = self.dl.readLearnedWeightsFromFile(rbm=True)
        self.userSimilarityRatings = loadedWeights[3] 
        self.densetrainingMatrix = loadedWeights[6] 
        self.weights = tf.Variable(tf.convert_to_tensor(loadedWeights[0], dtype=tf.float32, name="weights"))
        self.visibleBias = tf.Variable(tf.convert_to_tensor(loadedWeights[1], dtype=tf.float32, name="visibleBias"))
        self.hiddenBias = tf.Variable(tf.convert_to_tensor(loadedWeights[2], dtype=tf.float32, name="hiddenBias") )
        self.simWeights = tf.Variable(tf.convert_to_tensor(loadedWeights[4], dtype=tf.float32, name="similarityWeights"))
        self.simBias = tf.Variable(tf.convert_to_tensor(loadedWeights[5], dtype=tf.float32, name="similarityBias"))
        
       
    def GetName(self):
        return "RBM"