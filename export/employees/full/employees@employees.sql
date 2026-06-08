-- MySQLShell dump 2.0.1  Distrib Ver 8.0.46 for Win64 on x86_64 - for MySQL 8.0.46 (MySQL Community Server (GPL)), for Win64 (x86_64)
--
-- Host: localhost    Database: employees    Table: employees
-- ------------------------------------------------------
-- Server version	8.0.46

--
-- Table structure for table `employees`
--

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE IF NOT EXISTS `employees` (
  `emp_no` int NOT NULL,
  `birth_date` date NOT NULL,
  `first_name` varchar(14) NOT NULL,
  `last_name` varchar(16) NOT NULL,
  `gender` enum('M','F') NOT NULL,
  `hire_date` date NOT NULL,
  PRIMARY KEY (`emp_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
